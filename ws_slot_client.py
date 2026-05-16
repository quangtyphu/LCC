"""
Client WebSocket slot (Socket.IO EIO=4) — proxy SOCKS5 + JWT giống minigame.
- Engine.IO: nhận "2" → gửi "3" (ping/pong), ping_interval/timeout lấy từ handshake.
- Mất kết nối / timeout recv → đóng namespace → backoff → reconnect (đọc lại user từ API).
- Auto-spin: mỗi phiên WS mở lại luôn gọi ``refresh_jwt`` (login) rồi ghi JWT mới DB — không dùng JWT cache trước khi quay.
- Auto cược: 42/<namespace>,["spin",{"betId":...,"lines":[...]}] — mỗi lượt chỉ gửi sau khi xử lý xong spin-result trước (không bắn dồn khi accumulate/NV chậm).
- Mỗi spin-result → POST /api/slot-game-daily/accumulate (đếm một mã biểu tượng theo game, xem SlotGameProfile).
  SQLite `slot_game_daily_stats`: trong `CMS/server.js` — cột `slot_id` = **game preset** theo `--slot` (SLOT_PRESETS), không phải `roomId` trong tin WS.
- Đã reward_claimed trong ngày VN → không auto quay (chặn trước phiên + sau nhận thưởng); accumulate cùng ngày bị từ chối.
- Dừng auto-spin khi ``symbol_count`` CMS **sau xử lý NV** (kéo DB theo game nếu lệch) ≥ ``mission_progress_target`` (mặc định 600), hoặc khi ``reward_claimed=1``.
- Sang ngày VN mới: accumulate reset spin_count, symbol_count, reward_claimed về luồng ngày mới (CMS).
- Nhiệm vụ ngày: lần đầu CMS ≥ mốc NV trong phiên chờ ``MISSION_API_DELAY_AFTER_SYMBOL_CAP_SEC`` (30s) rồi GET mission game + đồng bộ/claim (API có thể chậm hơn DB).
- Một user chỉ một tiến trình slot (mọi --slot) tại một thời điểm — file lock `slot_client_locks/`.
- Mỗi spin-result (đã cược): ``bet_totals_increment.increment_bet_totals`` — cùng hàm / cùng bảng CMS với minigame (tổng cược gộp theo user, mọi game).
- ``postBalance`` trong spin-result → PUT ``/api/users/:username`` (DB local); auto-spin dừng khi số dư < ``--stop-below-balance`` (mặc định 100đ).
  Khi vậy: xếp nạp + ``POST /api/ws-slot-out-of-money`` tới Flask main (``LC79_MAIN_HTTP``, mặc định ``http://127.0.0.1:8080``) chỉ để loại khỏi ưu tiên SLOT_NV; **đồng thời** đóng WS slot (không ngắt WS tài xỉu).
- Kết thúc ``connect_slot`` (dừng hẳn): in ``[SESSION]`` tóm tắt cược/thắng (một lần; Ctrl+C ở ``main`` vẫn in ở ``finally`` nếu chưa in).

Chạy:
  python ws_slot_client.py user              # mặc định --slot 40
  python ws_slot_client.py user --slot 39
  python ws_slot_client.py user --slot 40
  python ws_slot_client.py user --no-spin
  python ws_slot_client.py user --no-slot-daily
  python ws_slot_client.py user --no-daily-mission
  python ws_slot_client.py user --bet-id 0 --bet-cost 100   # --lines mặc định: random 1 dòng trong 1–20
  python ws_slot_client.py user --stop-below-balance 100   # dừng quay khi postBalance < 100; 0 = tắt

Thêm game: bổ sung một SlotGameProfile trong SLOT_PRESETS (tls_host, namespace, count_symbol, daily_mission_name).
"""
from __future__ import annotations

import argparse
import gc
import random
import re
import sys
import asyncio
import json
import os
import time
import warnings
import contextlib
from pathlib import Path
import socks
import websockets
import requests
from dataclasses import dataclass

from jwt_manager import refresh_jwt
from mission_api import auto_claim_missions, fetch_missions
from bet_totals_increment import increment_bet_totals

API_BASE = "http://127.0.0.1:3000"
# Flask main (main.py run_flask) — ws_slot subprocess gọi để loại user khỏi ưu tiên SLOT_NV (không đụng WS minigame).
LC79_MAIN_HTTP = os.environ.get("LC79_MAIN_HTTP", "http://127.0.0.1:8080").rstrip("/")

# Sau khi CMS báo đủ symbol NV: chờ trước khi GET mission game (API có thể cập nhật chậm hơn DB).
MISSION_API_DELAY_AFTER_SYMBOL_CAP_SEC = 30

# Mặc định mốc symbol nhiệm vụ ngày (có thể override từng preset qua SlotGameProfile.mission_progress_target).
DEFAULT_MISSION_SYMBOL_TARGET = 600

# Một tin spin-result: in log nếu tổng totalMoneyWon (mọi block) ≥ ngưỡng (VND).
SPIN_WIN_LOG_MIN_VND = 5000

BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 60.0

SPIN_RANDOM_MIN_S = 0.1
SPIN_RANDOM_MAX_S = 0.2
# Chờ tối đa sau mỗi lần gửi spin nếu không thấy spin-result (tránh kẹt vòng lặp).
SPIN_RESULT_ACK_TIMEOUT_S = 120.0

# Sau mỗi spin-result: đồng nhất postBalance lên DB local; nếu đang auto-spin và số dư < ngưỡng → dừng (0 = tắt ngưỡng).
DEFAULT_STOP_SPIN_IF_BALANCE_BELOW_VND = 100


@dataclass(frozen=True)
class SpinOptions:
    bet_id: int
    lines: list[int]


@dataclass
class SpinSessionStats:
    """
    Tổng cược = spin_events × bet_cost; tổng trả thưởng = cộng dồn mọi totalMoneyWon (mỗi tin spin-result = 1 lần quay).
    ``nv_mission_api_delay_done``: đã chờ trước khi GET mission game lần đầu khi DB ≥ mốc NV (một lần/phiên).
    """

    spin_events: int = 0
    total_bet_vnd: int = 0
    total_won_vnd: int = 0
    summary_printed: bool = False
    nv_mission_api_delay_done: bool = False


@dataclass(frozen=True)
class SlotGameProfile:
    """Cấu hình một game slot: room_id = khóa preset/DB; WS host/namespace; biểu tượng đếm; NV ngày."""

    room_id: int
    tls_host: str
    namespace: str
    count_symbol: int
    daily_mission_name: str | None = None
    mission_progress_target: int = DEFAULT_MISSION_SYMBOL_TARGET

    def ws_url(self) -> str:
        """
        Giống browser/curl: wss://wslot39.tele68.com/slot39/?EIO=4&transport=websocket
        (đổi host/namespace theo bàn trong SLOT_PRESETS).
        """
        return f"wss://{self.tls_host}/{self.namespace}/?EIO=4&transport=websocket"


# iconId / mission name khớp response nhiệm vụ ngày từ game.
SLOT_PRESETS: dict[int, SlotGameProfile] = {
    39: SlotGameProfile(
        room_id=39,
        tls_host="wslot39.tele68.com",
        namespace="slot39",
        count_symbol=4,  # iconId 4 (VD: Jackpot — MISSION_DAILY_ICON_PLAY_RACING_SLOT39)
        daily_mission_name="MISSION_DAILY_ICON_PLAY_RACING_SLOT39",
    ),
    40: SlotGameProfile(
        room_id=40,
        tls_host="wslot40.tele68.com",
        namespace="slot40",
        count_symbol=0,  # iconId 0 (VD: biểu tượng Dưa hấu — MISSION_DAILY_ICON_PLAY_RACING_SLOT40)
        daily_mission_name="MISSION_DAILY_ICON_PLAY_RACING_SLOT40",
    ),
}
def get_slot_profile(room_id: int) -> SlotGameProfile:
    p = SLOT_PRESETS.get(int(room_id))
    if p is None:
        keys = ", ".join(str(k) for k in sorted(SLOT_PRESETS))
        raise KeyError(f"Chưa có preset slot room_id={room_id}. Thêm vào SLOT_PRESETS: {keys}")
    return p


def _slot_lock_stem(username: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", (username or "").strip())
    return (s or "user")[:180]


def _slot_lock_path(username: str) -> Path:
    return Path(__file__).resolve().parent / "slot_client_locks" / f"{_slot_lock_stem(username)}.lock"


class _OneUserOneSlotLock:
    """
    Một user chỉ một tiến trình ws_slot_client tại một thời điểm (mọi --slot).
    Dùng khóa file OS (Windows: msvcrt; Unix: fcntl) — process chết thì lock hết.
    """

    __slots__ = ("username", "slot_id", "_fp", "_path")

    def __init__(self, username: str, slot_id: int) -> None:
        self.username = username
        self.slot_id = int(slot_id)
        self._fp = None
        self._path = _slot_lock_path(username)

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = open(self._path, "a+b", buffering=0)
        try:
            if sys.platform == "win32":
                import msvcrt

                self._fp.seek(0)
                msvcrt.locking(self._fp.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            self._fp.close()
            self._fp = None
            raise RuntimeError(
                f"[{self.username}] Đang chạy ws_slot_client khác — chỉ được 1 game slot / user. "
                f"Tắt instance kia hoặc chờ thoát."
            ) from e

        meta = (
            json.dumps(
                {
                    "username": self.username,
                    "slot": self.slot_id,
                    "pid": os.getpid(),
                    "ts": time.time(),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        self._fp.seek(0)
        self._fp.write(meta.encode("utf-8"))
        self._fp.truncate()
        self._fp.flush()

    def release(self) -> None:
        if self._fp is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._fp.seek(0)
                msvcrt.locking(self._fp.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fp.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        with contextlib.suppress(Exception):
            self._fp.close()
        self._fp = None


def user_slot_client_lock_busy(username: str) -> bool:
    """
    True nếu đã có tiến trình ws_slot_client khác giữ lock cho user (không spawn thêm).
    Thử acquire không chặn rồi release ngay nếu thành công — subprocess sau đó tự lock lại.
    """
    lock = _OneUserOneSlotLock(username, 40)
    try:
        lock.acquire()
    except RuntimeError:
        return True
    lock.release()
    return False


def _parse_lines_arg(s: str) -> list[int]:
    parts = [p.strip() for p in s.replace(" ", "").split(",") if p.strip()]
    return [int(x) for x in parts]


def default_random_spin_line_one() -> int:
    """Một dòng 1–20 khi không truyền ``--lines`` (CLI + SLOT_NV subprocess)."""
    return random.randint(1, 20)


def _spin_socket_payload(opt: SpinOptions, namespace: str) -> str:
    body = ["spin", {"betId": opt.bet_id, "lines": opt.lines}]
    return f"42/{namespace},{json.dumps(body, separators=(',', ':'))}"


async def _wait_spin_result_ack_or_stop(ack: asyncio.Event, stop: asyncio.Event) -> None:
    """Chờ handler xử lý xong một spin-result (ack) hoặc stop (không gửi lượt mới trước khi ack)."""
    if stop.is_set() or ack.is_set():
        return
    t_ack = asyncio.create_task(ack.wait())
    t_stop = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait(
            {t_ack, t_stop},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for t in (t_ack, t_stop):
            if not t.done():
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t


async def _auto_spin_loop(
    ws,
    username: str,
    opt: SpinOptions,
    stop: asyncio.Event,
    namespace: str,
    spin_result_ack: asyncio.Event | None = None,
):
    """Gửi spin lặp lại; mỗi lượt chỉ gửi sau khi đã xử lý xong spin-result trước (tránh bắn dồn khi accumulate/NV chậm)."""
    frame = _spin_socket_payload(opt, namespace)
    await asyncio.sleep(0.3)
    while not stop.is_set():
        wait_s = random.uniform(SPIN_RANDOM_MIN_S, SPIN_RANDOM_MAX_S)
        try:
            if not getattr(ws, "open", True):
                break
            if spin_result_ack is not None:
                spin_result_ack.clear()
            await ws.send(frame)
        except Exception as e:
            print(f"⚠️ [{username}] auto-spin gửi lỗi: {e}", flush=True)
            break
        if spin_result_ack is not None:
            try:
                await asyncio.wait_for(
                    _wait_spin_result_ack_or_stop(spin_result_ack, stop),
                    timeout=SPIN_RESULT_ACK_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                print(
                    f"⚠️ [{username}] {SPIN_RESULT_ACK_TIMEOUT_S:.0f}s không có spin-result — dừng auto-spin",
                    flush=True,
                )
                stop.set()
                break
            if stop.is_set():
                break
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait_s)
            break
        except asyncio.TimeoutError:
            continue


# Handshake giống curl DevTools (slot39/40 — chỉ đổi host + path /slotN/)
SLOT_WS_ORIGIN = "https://lc79b.bet"
SLOT_WS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)


if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _slot_low_balance_side_effects(username: str, bal: int, threshold: int) -> None:
    """
    Hết tiền giữa phiên slot NV: xếp nạp + báo main để loại khỏi ưu tiên SLOT_NV.
    (Đóng WS slot: coroutine ``gather`` song song với ``to_thread`` bọc hàm này.)
    """
    try:
        from auto_deposit_on_out_of_money import try_enqueue_deposit_if_cache_allows

        ok = try_enqueue_deposit_if_cache_allows(
            username,
            f"[ws_slot] postBalance {bal:,}đ < {threshold:,}đ sau spin NV",
        )
        if ok:
            print(
                f"[{_ts()}] 💳 [{username}] Đã xếp lệnh nạp (hàng chờ auto-deposit).",
                flush=True,
            )
        else:
            print(
                f"[{_ts()}] 💳 [{username}] Chưa xếp nạp (cache treo / đã trong hàng chờ).",
                flush=True,
            )
    except Exception as e:
        print(f"[{_ts()}] ⚠️ [{username}] Xếp nạp: {e}", flush=True)
    try:
        r = requests.post(
            f"{LC79_MAIN_HTTP}/api/ws-slot-out-of-money",
            json={"username": username},
            timeout=4,
        )
        if r.status_code != 200:
            print(
                f"[{_ts()}] ⚠️ [{username}] Báo main hết tiền slot HTTP {r.status_code}: "
                f"{(r.text or '')[:120]}",
                flush=True,
            )
    except Exception as e:
        print(
            f"[{_ts()}] ⚠️ [{username}] Không gọi được main ({LC79_MAIN_HTTP}) để loại SLOT_NV: {e}",
            flush=True,
        )


async def _close_open_slot_ws(ws) -> None:
    if ws is not None and getattr(ws, "open", False):
        with contextlib.suppress(Exception):
            await ws.close()


def _build_proxy(proxy_str: str):
    host, port, userp, passp = proxy_str.split(":")
    return host, int(port), userp, passp


def _fetch_user(username: str):
    try:
        r = requests.get(f"{API_BASE}/api/users/{username}", timeout=5)
        if r.status_code != 200:
            print(f"❌ [{username}] API users HTTP {r.status_code}", flush=True)
            return None
        return r.json()
    except Exception as e:
        print(f"⚠️ [{username}] Lỗi lấy user từ DB: {e}", flush=True)
        return None


def _update_jwt(username: str, jwt_token: str):
    try:
        requests.put(f"{API_BASE}/api/users/{username}", json={"jwt": jwt_token}, timeout=5)
    except Exception:
        pass


async def _ensure_jwt(
    username: str, user_data: dict, *, force_refresh: bool = False
) -> str | None:
    jwt_token = (user_data.get("jwt") or "").strip()
    if jwt_token and not force_refresh:
        return jwt_token
    if force_refresh:
        print(
            f"🔄 [{username}] Refresh JWT trước phiên slot (auto-spin)...",
            flush=True,
        )
    else:
        print(f"⚠️ [{username}] Không có JWT, thử refresh...", flush=True)
    jwt_token = await asyncio.to_thread(lambda: refresh_jwt(username))
    if jwt_token:
        _update_jwt(username, jwt_token)
    return jwt_token


def _matrix_columns_to_rows(cols: list) -> list[list]:
    """API trả matrix dạng list cột, mỗi cột [sym0,sym1,sym2] → 3 hàng × N cột."""
    if not cols or not isinstance(cols, list):
        return []
    try:
        n_col = len(cols)
        n_row = len(cols[0])
        return [[cols[c][r] for c in range(n_col)] for r in range(n_row)]
    except (IndexError, TypeError):
        return []


def _count_symbol_in_matrix_cols(matrix_cols: list | None, symbol: int) -> int:
    """Matrix dạng list cột, mỗi cột là list ô."""
    if not matrix_cols or not isinstance(matrix_cols, list):
        return 0
    n = 0
    for col in matrix_cols:
        if not isinstance(col, list):
            continue
        for cell in col:
            try:
                if int(cell) == symbol:
                    n += 1
            except (TypeError, ValueError):
                if cell == symbol:
                    n += 1
    return n


def _count_symbol_in_spin_result_payload(data: dict, symbol: int) -> int:
    """Một lần bấm quay có thể có nhiều lượt (spinResults); đếm trên tất cả matrix."""
    total = 0
    for block in data.get("spinResults") or []:
        if isinstance(block, dict):
            total += _count_symbol_in_matrix_cols(block.get("matrix"), symbol)
    return total


def _post_slot_game_daily_accumulate(
    username: str,
    slot_id: int,
    delta_spin: int,
    delta_symbol: int,
    delta_reward: int = 0,
) -> dict | None:
    """Cộng dồn vào slot_game_daily_stats. `slot_id` = game preset (--slot, khớp SLOT_PRESETS)."""
    dr = max(0, int(delta_reward))
    if delta_spin <= 0 and delta_symbol <= 0 and dr <= 0:
        return {}
    try:
        r = requests.post(
            f"{API_BASE}/api/slot-game-daily/accumulate",
            json={
                "username": username,
                "slot_id": slot_id,
                "delta_spin": delta_spin,
                "delta_symbol": delta_symbol,
                "delta_reward": dr,
            },
            timeout=5,
        )
        if r.status_code != 200:
            print(
                f"⚠️ [{username}] slot_game_daily_stats: HTTP {r.status_code} {r.text[:160]}",
                flush=True,
            )
            return None
        try:
            return r.json()
        except Exception:
            return {}
    except Exception as e:
        print(f"⚠️ [{username}] slot_game_daily_stats: {e}", flush=True)
        return None


def _post_slot_game_daily_set_symbol_count(
    username: str, slot_id: int, symbol_count: int
) -> dict | None:
    """Ghi đè symbol_count (đồng bộ khi CMS > progress API)."""
    try:
        r = requests.post(
            f"{API_BASE}/api/slot-game-daily/set-symbol-count",
            json={
                "username": username,
                "slot_id": slot_id,
                "symbol_count": int(symbol_count),
            },
            timeout=5,
        )
        if r.status_code != 200:
            print(
                f"⚠️ [{username}] set-symbol-count: HTTP {r.status_code} {r.text[:160]}",
                flush=True,
            )
            return None
        try:
            return r.json()
        except Exception:
            return {}
    except Exception as e:
        print(f"⚠️ [{username}] set-symbol-count: {e}", flush=True)
        return None


def _post_slot_game_daily_set_reward_claimed(
    username: str, slot_id: int, reward_claimed: int = 1
) -> dict | None:
    """CMS slot_game_daily_stats.reward_claimed (0/1)."""
    try:
        r = requests.post(
            f"{API_BASE}/api/slot-game-daily/set-reward-claimed",
            json={
                "username": username,
                "slot_id": slot_id,
                "reward_claimed": int(1 if reward_claimed else 0),
            },
            timeout=5,
        )
        if r.status_code != 200:
            print(
                f"⚠️ [{username}] set-reward-claimed: HTTP {r.status_code} {r.text[:160]}",
                flush=True,
            )
            return None
        try:
            return r.json()
        except Exception:
            return {}
    except Exception as e:
        print(f"⚠️ [{username}] set-reward-claimed: {e}", flush=True)
        return None


def _get_slot_game_daily_stats(username: str, slot_id: int) -> dict | None:
    """GET stats; `slot_id` = game preset (--slot)."""
    try:
        r = requests.get(
            f"{API_BASE}/api/slot-game-daily/stats",
            params={"username": username, "slot_id": slot_id},
            timeout=5,
        )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _slot_reward_blocks_play(username: str, room_id: int) -> bool:
    """Đã nhận thưởng NV slot trong ngày VN hiện tại → không auto quay."""
    j = _get_slot_game_daily_stats(username, room_id)
    if not j or not j.get("ok"):
        return False
    if j.get("empty"):
        return False
    try:
        return int(j.get("reward_claimed") or 0) == 1
    except (TypeError, ValueError):
        return False


def _maybe_sync_mission_and_claim_daily(
    username: str,
    db_symbol_count: int,
    profile: SlotGameProfile,
    *,
    allow_claim: bool = True,
) -> tuple[bool, int]:
    """
    Chạy khi ``symbol_count`` (CMS) sau tin quay đã ≥ mốc NV: đối chiếu GET mission
    ``profile.daily_mission_name``.

    - CMS ≥ mốc NV (``db_sym >= threshold``) nhưng game báo **chưa** tới mốc (``api_cur < threshold``)
      và ``db_sym > api_cur`` → **luôn** set-symbol-count hạ CMS theo game (tránh CMS vượt game).
    - Game đã ≥ mốc (``api_cur >= threshold``) → **không** hạ CMS (vd. 600/600 hoặc game đã đủ).

    Khi ``allow_claim=True``: đủ điều kiện → ``auto_claim_missions`` (có thể claim nhiệm vụ **khác**
    trong ngày) + ghi ``reward_claimed`` CMS cho bàn này.

    Trả về ``(đã ghi reward_claimed=1, symbol_count CMS sau xử lý NV)``.
    """
    name = profile.daily_mission_name
    if not name:
        return (False, int(db_symbol_count))
    threshold = int(profile.mission_progress_target)
    if db_symbol_count < threshold:
        return (False, int(db_symbol_count))

    result = fetch_missions(username, "daily")
    if not result.get("ok"):
        return (False, int(db_symbol_count))

    missions = result.get("data") or []
    m = next(
        (x for x in missions if isinstance(x, dict) and x.get("name") == name),
        None,
    )
    if not m:
        return (False, int(db_symbol_count))

    prog = m.get("progress") or []
    try:
        api_cur = int(float(prog[0])) if len(prog) > 0 else 0
    except (TypeError, ValueError):
        api_cur = 0
    try:
        api_target = int(float(prog[1])) if len(prog) > 1 else threshold
    except (TypeError, ValueError):
        api_target = threshold

    game_slot = int(profile.room_id)
    sym = profile.count_symbol
    db_sym = int(db_symbol_count)
    # Kéo CMS theo game khi CMS đã ≥ mốc NV nhưng progress nhiệm vụ game vẫn < mốc (không phụ thuộc api_target).
    if db_sym > api_cur and db_sym >= threshold and api_cur < threshold:
        sync_body = _post_slot_game_daily_set_symbol_count(username, game_slot, api_cur)
        if sync_body is not None:
            print(
                f"[{_ts()}] SLOT_NV game_slot={game_slot} icon={sym} "
                f"DB {db_sym} → API {api_cur} (mốc NV {api_target})",
                flush=True,
            )
        db_sym = api_cur

    if db_sym < threshold:
        return (False, db_sym)
    if m.get("claimedAt"):
        return (False, db_sym)
    if not allow_claim:
        return (False, db_sym)

    done = bool(m.get("isWon")) or (api_target > 0 and api_cur >= api_target)
    if not done:
        return (False, db_sym)

    claimed_names = auto_claim_missions(username)
    marked = name in claimed_names
    if not marked:
        result2 = fetch_missions(username, "daily")
        if result2.get("ok"):
            m2 = next(
                (
                    x
                    for x in (result2.get("data") or [])
                    if isinstance(x, dict) and x.get("name") == name
                ),
                None,
            )
            marked = bool(m2 and m2.get("claimedAt"))

    if marked:
        body = _post_slot_game_daily_set_reward_claimed(username, game_slot, 1)
        if body is not None:
            print(
                f"[{_ts()}] SLOT_DAILY reward_claimed=1 game_slot={game_slot} ({name})",
                flush=True,
            )
            return (True, db_sym)
    return (False, db_sym)


def _sum_total_money_won_from_payload(payload: dict) -> int:
    """Tổng totalMoneyWon mọi block spinResults trong một tin spin-result (số nguyên VND)."""
    total = 0
    for block in payload.get("spinResults") or []:
        if not isinstance(block, dict):
            continue
        tw = block.get("totalMoneyWon")
        if tw is None:
            continue
        try:
            total += int(round(float(tw)))
        except (TypeError, ValueError):
            pass
    return max(0, total)


def _spin_message_delta_and_blocks(payload: dict) -> tuple[int, list]:
    """Số lượt quay trong một tin spin-result + list block hợp lệ (khớp delta_spin khi ghi slot_daily)."""
    blocks = [b for b in (payload.get("spinResults") or []) if isinstance(b, dict)]
    n = len(blocks) if blocks else 1
    return n, blocks


def _add_spin_result_to_session_stats(
    payload: dict,
    stats: SpinSessionStats,
    bet_cost_vnd: int,
) -> None:
    """Mỗi tin spin-result: + (bet_cost × số block spinResults, tối thiểu 1) vào tổng cược; + totalMoneyWon."""
    n, _ = _spin_message_delta_and_blocks(payload)
    cost = max(0, int(bet_cost_vnd)) * n
    stats.spin_events += n
    stats.total_bet_vnd += cost
    stats.total_won_vnd += _sum_total_money_won_from_payload(payload)


def _post_balance_to_int(raw) -> int | None:
    if raw is None:
        return None
    try:
        return int(round(float(raw)))
    except (TypeError, ValueError):
        return None


def _sync_user_balance_to_local_db(username: str, balance: int) -> None:
    """PUT /api/users/:username { balance } — cùng Node API với ws_events / get_balance."""
    try:
        r = requests.put(
            f"{API_BASE}/api/users/{username}",
            json={"balance": balance},
            timeout=5,
        )
        if r.status_code != 200:
            print(
                f"⚠️ [{username}] Đồng bộ số dư DB HTTP {r.status_code}: {(r.text or '')[:240]}",
                flush=True,
            )
    except Exception as e:
        print(f"⚠️ [{username}] Đồng bộ số dư DB: {e}", flush=True)


def _print_spin_session_summary(username: str, stats: SpinSessionStats, bet_cost_vnd: int) -> None:
    if stats.summary_printed:
        return
    stats.summary_printed = True
    if stats.spin_events <= 0:
        return
    net = stats.total_won_vnd - stats.total_bet_vnd
    n = stats.spin_events
    c = bet_cost_vnd
    print(
        f"\n[SESSION] [{username}] Tóm tắt: "
        f"tổng cược = {stats.total_bet_vnd:,}đ ({n} lượt; --bet-cost {c}đ mỗi lượt trong tin) | "
        f"tổng trả thưởng = tổng totalMoneyWon = {stats.total_won_vnd:,}đ | "
        f"lãi/lỗ = {net:,}đ",
        flush=True,
    )


async def _handle_slot_incoming(
    username: str,
    msg,
    profile: SlotGameProfile,
    *,
    sync_slot_daily_db: bool,
    daily_mission: bool = True,
    stop_spin: asyncio.Event | None = None,
    spin_result_ack: asyncio.Event | None = None,
    session_stats: SpinSessionStats | None = None,
    bet_cost_vnd: int = 100,
    stop_spin_if_balance_below: int = DEFAULT_STOP_SPIN_IF_BALANCE_BELOW_VND,
    low_balance_session_stop: asyncio.Event | None = None,
    ws=None,
    nv_spin_goal_done: asyncio.Event | None = None,
) -> None:
    """spin-result: in log + (tuỳ chọn) ghi slot_game_daily_stats (spin + đếm biểu tượng theo profile)."""
    if not isinstance(msg, str):
        return
    evt_prefix = f"42/{profile.namespace},"
    if not msg.startswith(evt_prefix):
        return
    try:
        inner = json.loads(msg[len(evt_prefix) :])
    except Exception:
        return
    if not isinstance(inner, list) or len(inner) < 2:
        return
    if inner[0] != "spin-result":
        return
    payload = inner[1]
    if not isinstance(payload, dict):
        if spin_result_ack is not None:
            spin_result_ack.set()
        return
    try:
        bal_int = _post_balance_to_int(payload.get("postBalance"))
        if bal_int is not None:
            await asyncio.to_thread(_sync_user_balance_to_local_db, username, bal_int)
        if (
            stop_spin is not None
            and stop_spin_if_balance_below > 0
            and bal_int is not None
            and bal_int < stop_spin_if_balance_below
        ):
            print(
                f"[{_ts()}] ⏹️ [{username}] Dừng quay: số dư {bal_int:,}đ < {stop_spin_if_balance_below:,}đ",
                flush=True,
            )
            await asyncio.gather(
                asyncio.to_thread(
                    _slot_low_balance_side_effects,
                    username,
                    bal_int,
                    stop_spin_if_balance_below,
                ),
                _close_open_slot_ws(ws),
            )
            stop_spin.set()
            if low_balance_session_stop is not None:
                low_balance_session_stop.set()
        delta_spin, results_list = _spin_message_delta_and_blocks(payload)
        inc_bet = max(0, int(bet_cost_vnd)) * delta_spin
        if inc_bet > 0:
            await asyncio.to_thread(increment_bet_totals, username, inc_bet)
        if session_stats is not None:
            _add_spin_result_to_session_stats(payload, session_stats, bet_cost_vnd)
        won_vnd = _sum_total_money_won_from_payload(payload)
        if won_vnd >= SPIN_WIN_LOG_MIN_VND:
            spin_id = payload.get("spinId", "")
            print(
                f"[{_ts()}] 🎰 [{username}] Thắng {won_vnd:,}đ "
                f"(≥ {SPIN_WIN_LOG_MIN_VND:,}đ) spinId={spin_id}",
                flush=True,
            )
        if not sync_slot_daily_db:
            return
        n_sym = _count_symbol_in_spin_result_payload(payload, profile.count_symbol)
        spin_id = payload.get("spinId", "")
        try:
            ws_room = int(payload.get("roomId"))
        except (TypeError, ValueError):
            ws_room = 0
        game_slot = int(profile.room_id)

        delta_rw = won_vnd
        body = await asyncio.to_thread(
            _post_slot_game_daily_accumulate,
            username,
            game_slot,
            delta_spin,
            n_sym,
            delta_rw,
        )
        if body is not None:
            if body.get("blocked") and body.get("reason") == "reward_claimed":
                print(
                    f"[{_ts()}] SLOT_DAILY đã nhận thưởng hôm nay — dừng quay game_slot={game_slot} "
                    f"ws_room={ws_room} (spinId={spin_id})",
                    flush=True,
                )
                if stop_spin is not None:
                    stop_spin.set()
                if nv_spin_goal_done is not None:
                    nv_spin_goal_done.set()
                return

            sy = body.get("symbol_count")
            rc = body.get("reward_claimed")
            sy_db: int | None = None
            if sy is not None:
                try:
                    sy_db = int(sy)
                except (TypeError, ValueError):
                    sy_db = None

            nv_cap = int(profile.mission_progress_target)

            stop_after = False
            sy_after_nv = sy_db
            if sy_db is not None and not body.get("skipped") and (
                daily_mission or sy_db >= nv_cap
            ):
                if (
                    sy_db >= nv_cap
                    and session_stats is not None
                    and not session_stats.nv_mission_api_delay_done
                ):
                    session_stats.nv_mission_api_delay_done = True
                    print(
                        f"[{_ts()}] [{username}] DB symbol_count ≥ {nv_cap} — chờ "
                        f"{MISSION_API_DELAY_AFTER_SYMBOL_CAP_SEC}s rồi mới gọi API nhiệm vụ game "
                        f"(game có thể cập nhật chậm hơn CMS).",
                        flush=True,
                    )
                    await asyncio.sleep(MISSION_API_DELAY_AFTER_SYMBOL_CAP_SEC)
                stop_after, sy_after_nv = await asyncio.to_thread(
                    _maybe_sync_mission_and_claim_daily,
                    username,
                    sy_db,
                    profile,
                    allow_claim=daily_mission,
                )
            try:
                rc_int = int(rc) if rc is not None else 0
            except (TypeError, ValueError):
                rc_int = 0
            if stop_after or rc_int == 1:
                if stop_spin is not None:
                    stop_spin.set()
                if nv_spin_goal_done is not None:
                    nv_spin_goal_done.set()
                print(
                    f"[{_ts()}] SLOT_DAILY reward_claimed=1 — dừng quay game_slot={game_slot}",
                    flush=True,
                )
            elif (
                stop_spin is not None
                and sy_after_nv is not None
                and sy_after_nv >= nv_cap
            ):
                stop_spin.set()
                if nv_spin_goal_done is not None:
                    nv_spin_goal_done.set()
                print(
                    f"[{_ts()}] ⏹️ [{username}] Dừng quay: symbol_count CMS (sau NV) = {sy_after_nv} "
                    f"≥ mốc NV {nv_cap} (game_slot={game_slot})",
                    flush=True,
                )
    finally:
        if spin_result_ack is not None:
            spin_result_ack.set()


async def _close_ws_clean(ws, sock, namespace: str):
    if ws is not None:
        with contextlib.suppress(Exception):
            if ws.open:
                await ws.send(f"41/{namespace}")
                await asyncio.sleep(0.5)
                with contextlib.suppress(OSError):
                    await asyncio.wait_for(ws.close(), timeout=2.0)
                wait_closed = getattr(ws, "wait_closed", None)
                if callable(wait_closed):
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(wait_closed(), timeout=3.0)
    with contextlib.suppress(OSError, Exception):
        sock.close()


def _asyncio_run_slot_main(main_coro) -> None:
    """
    Chạy ``connect_slot`` như ``asyncio.run``.

    Trên Windows, đóng WebSocket sau phiên có thể khiến selector báo lỗi socket khi
    ``Runner`` gọi ``shutdown_asyncgens``; CPython đôi khi phát ``RuntimeWarning``
    coroutine chưa await — lọc đúng cảnh báo đó (không ẩn RuntimeWarning khác theo message).
    """
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"coroutine .*shutdown_asyncgens.*was never awaited",
            category=RuntimeWarning,
        )
        try:
            asyncio.run(main_coro)
        finally:
            gc.collect()


async def _slot_one_session(
    username: str,
    profile: SlotGameProfile,
    spin: SpinOptions | None = None,
    *,
    sync_slot_daily_db: bool = True,
    daily_mission: bool = True,
    session_stats: SpinSessionStats | None = None,
    bet_cost_vnd: int = 100,
    stop_spin_if_balance_below: int = DEFAULT_STOP_SPIN_IF_BALANCE_BELOW_VND,
) -> tuple[str, bool]:
    """
    Một lần mở WS tới slot. Trả về (outcome, reset_backoff):
      outcome "reconnect" — thử lại sau backoff
      outcome "stop"      — dừng hẳn (thiếu user/proxy/jwt / số dư thấp / NV xong bàn này: reward_claimed hoặc đủ symbol)
      reset_backoff True  — phiên giữ được ≥15s có nhận dữ liệu → reset backoff về tối thiểu
    Auto-spin: mỗi phiên luôn gọi refresh_jwt trước khi mở WS (không dùng JWT cache DB).
    """
    user = _fetch_user(username)
    if not user:
        return ("stop", False)

    proxy_str = (user.get("proxy") or "").strip()
    if not proxy_str:
        print(f"❌ [{username}] Thiếu proxy trong DB.", flush=True)
        return ("stop", False)

    jwt_force = spin is not None
    jwt_token = await _ensure_jwt(username, user, force_refresh=jwt_force)
    if not jwt_token:
        print(f"❌ [{username}] Không có JWT.", flush=True)
        return ("stop", False)

    if (
        spin is not None
        and sync_slot_daily_db
        and await asyncio.to_thread(
            _slot_reward_blocks_play, username, profile.room_id
        )
    ):
        print(
            f"⏹️ [{username}] Slot game_slot={profile.room_id}: đã nhận thưởng NV hôm nay "
            f"(reward_claimed=1) — không quay; sang ngày VN mới accumulate sẽ reset các trường.",
            flush=True,
        )
        return ("stop", False)

    try:
        host, port, puser, ppass = _build_proxy(proxy_str)
    except Exception as e:
        print(f"❌ [{username}] Proxy format lỗi: {e}", flush=True)
        return ("reconnect", False)

    tls_host = profile.tls_host
    ws_url = profile.ws_url()
    ns = profile.namespace

    sock = socks.socksocket()
    sock.set_proxy(socks.SOCKS5, host, port, True, puser, ppass)
    sock.setblocking(False)
    try:
        sock.connect((tls_host, 443))
    except Exception as e:
        print(f"❌ [{username}] TCP+proxy tới {tls_host}:443: {e}", flush=True)
        return ("reconnect", False)

    ws = None
    try:
        # Không truyền additional_headers khi dùng sock= (SOCKS): websockets 14+ có thể
        # chuyển nhầm kwargs → BaseEventLoop.create_connection(..., additional_headers=...).
        ws = await websockets.connect(
            ws_url,
            sock=sock,
            ssl=True,
            server_hostname=tls_host,
            origin=SLOT_WS_ORIGIN,
            user_agent_header=SLOT_WS_USER_AGENT,
            compression="deflate",
            ping_interval=None,
        )
    except Exception as e:
        print(f"❌ [{username}] Lỗi mở WebSocket: {e}", flush=True)
        with contextlib.suppress(Exception):
            sock.close()
        return ("reconnect", False)

    recv_any = False
    t_after_auth: float | None = None
    low_balance_session_stop: asyncio.Event | None = None
    nv_spin_goal_done: asyncio.Event | None = None
    try:
        ping_interval_ms = 25000
        ping_timeout_ms = 20000
        recv_timeout = 45.0
        try:
            handshake = await asyncio.wait_for(ws.recv(), timeout=5)
            if isinstance(handshake, str) and handshake.startswith("0"):
                payload = json.loads(handshake[1:])
                ping_interval_ms = int(payload.get("pingInterval", ping_interval_ms))
                ping_timeout_ms = int(payload.get("pingTimeout", ping_timeout_ms))
                recv_timeout = ping_interval_ms / 1000 + ping_timeout_ms / 1000 + 5
        except Exception as e:
            print(f"⚠️ [{username}] Handshake Engine.IO: {e}", flush=True)
            recv_timeout = 45.0

        auth_payload = f"40/{ns},{json.dumps({'token': jwt_token})}"
        await ws.send(auth_payload)
        t_after_auth = time.monotonic()

        low_balance_session_stop = asyncio.Event() if spin is not None else None
        nv_spin_goal_done = asyncio.Event() if spin is not None else None
        stop_spin = asyncio.Event()
        spin_result_ack: asyncio.Event | None = (
            asyncio.Event() if spin is not None else None
        )
        spin_task: asyncio.Task | None = None
        if spin is not None:
            spin_task = asyncio.create_task(
                _auto_spin_loop(ws, username, spin, stop_spin, ns, spin_result_ack)
            )

        try:
            # Giống minigame: recv có timeout; "2" → "3"; mọi lỗi/timeout → thoát phiên → reconnect
            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
                except asyncio.TimeoutError:
                    print(
                        f"⚠️ [{username}] recv im lặng quá {recv_timeout:.1f}s — thoát phiên "
                        f"(không nhận ping Engine.IO / tin từ server).",
                        flush=True,
                    )
                    break
                except websockets.exceptions.ConnectionClosed as e:
                    print(f"⚠️ [{username}] WebSocket đóng: {e}", flush=True)
                    break
                except Exception as e:
                    print(f"⚠️ [{username}] recv lỗi: {e!r}", flush=True)
                    break

                recv_any = True
                if isinstance(msg, str) and msg.startswith("42/") and not msg.startswith(
                    f"42/{profile.namespace},"
                ):
                    head = msg[:160] + ("…" if len(msg) > 160 else "")
                    print(
                        f"⚠️ [{username}] tin 42/ khác namespace (đang preset {profile.namespace}): {head}",
                        flush=True,
                    )
                if msg == "2":
                    with contextlib.suppress(Exception):
                        await ws.send("3")
                    continue

                await _handle_slot_incoming(
                    username,
                    msg,
                    profile,
                    sync_slot_daily_db=sync_slot_daily_db,
                    daily_mission=daily_mission,
                    stop_spin=stop_spin,
                    spin_result_ack=spin_result_ack,
                    session_stats=session_stats,
                    bet_cost_vnd=bet_cost_vnd,
                    stop_spin_if_balance_below=stop_spin_if_balance_below,
                    low_balance_session_stop=low_balance_session_stop,
                    ws=ws,
                    nv_spin_goal_done=nv_spin_goal_done,
                )
                if (
                    spin is not None
                    and nv_spin_goal_done is not None
                    and nv_spin_goal_done.is_set()
                ):
                    break
        finally:
            stop_spin.set()
            if spin_task is not None:
                if not spin_task.done():
                    spin_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await spin_task
            # Cho loop xử lý hủy trước khi đóng WS (giảm WinError 10038 trên Windows).
            await asyncio.sleep(0.05)

    except Exception as e:
        print(f"❌ [{username}] Lỗi phiên WS: {e}", flush=True)
    finally:
        await _close_ws_clean(ws, sock, ns)

    if low_balance_session_stop is not None and low_balance_session_stop.is_set():
        return ("stop", False)

    if nv_spin_goal_done is not None and nv_spin_goal_done.is_set():
        return ("stop", False)

    stable = bool(
        recv_any and t_after_auth is not None and (time.monotonic() - t_after_auth >= 15.0)
    )
    return ("reconnect", stable)


async def connect_slot(
    username: str,
    profile: SlotGameProfile,
    spin: SpinOptions | None = None,
    *,
    sync_slot_daily_db: bool = True,
    daily_mission: bool = True,
    session_stats: SpinSessionStats | None = None,
    bet_cost_vnd: int = 100,
    stop_spin_if_balance_below: int = DEFAULT_STOP_SPIN_IF_BALANCE_BELOW_VND,
):
    backoff = BACKOFF_MIN_S
    while True:
        try:
            outcome, reset_backoff = await _slot_one_session(
                username,
                profile,
                spin=spin,
                sync_slot_daily_db=sync_slot_daily_db,
                daily_mission=daily_mission,
                session_stats=session_stats,
                bet_cost_vnd=bet_cost_vnd,
                stop_spin_if_balance_below=stop_spin_if_balance_below,
            )
        except asyncio.CancelledError:
            raise

        if outcome == "stop":
            if session_stats is not None:
                _print_spin_session_summary(username, session_stats, bet_cost_vnd)
            break

        if reset_backoff:
            backoff = BACKOFF_MIN_S

        print(
            f"↻ [{username}] Kết thúc phiên — reconnect sau {backoff:.1f}s (Ctrl+C để dừng)",
            flush=True,
        )
        try:
            await asyncio.sleep(backoff)
        except asyncio.CancelledError:
            raise
        backoff = min(backoff * 2, BACKOFF_MAX_S)


def main():
    preset_keys = sorted(SLOT_PRESETS.keys())
    parser = argparse.ArgumentParser(description="WS slot lc79 (Socket.IO) — proxy + JWT")
    parser.add_argument("username", nargs="?", help="Username trong DB Node API")
    parser.add_argument(
        "--slot",
        type=int,
        default=40,
        choices=preset_keys,
        help=f"Bàn slot (preset có sẵn: {preset_keys})",
    )
    parser.add_argument(
        "--no-spin",
        action="store_true",
        help="Không tự gửi spin (chỉ giữ kết nối WS; không in log từng spin-result)",
    )
    parser.add_argument(
        "--spin",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--bet-id", type=int, default=0, dest="bet_id")
    parser.add_argument(
        "--bet-cost",
        type=int,
        default=100,
        dest="bet_cost_vnd",
        help="VND/lượt quay — tổng cược in cuối phiên = số lần quay × giá trị này (mặc định 100)",
    )
    parser.add_argument(
        "--lines",
        type=str,
        default=None,
        help='Danh sách line, VD "1" hoặc "1,2,3". Mặc định (không truyền): một dòng ngẫu nhiên 1–20.',
    )
    parser.add_argument(
        "--no-slot-daily",
        action="store_true",
        help="Không ghi slot_game_daily_stats",
    )
    parser.add_argument(
        "--no-symbol6-db",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--no-daily-mission",
        action="store_true",
        help="Không đồng bộ/claim nhiệm vụ ngày (theo preset --slot)",
    )
    parser.add_argument(
        "--no-slot40-mission",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--stop-below-balance",
        type=int,
        default=DEFAULT_STOP_SPIN_IF_BALANCE_BELOW_VND,
        metavar="N",
        help="Sau spin-result: PUT postBalance lên DB; nếu đang auto-spin và số dư < Nđ thì dừng (mặc định 100; 0 = không dừng theo số dư)",
    )
    args = parser.parse_args()
    no_slot_daily = args.no_slot_daily or args.no_symbol6_db
    daily_mission = (
        (not args.no_daily_mission)
        and (not args.no_slot40_mission)
        and (not no_slot_daily)
    )

    username = (args.username or "").strip()
    if not username:
        username = input("Nhập username: ").strip()
    if not username:
        print("❌ Chưa nhập username")
        return

    spin: SpinOptions | None = None
    if not args.no_spin:
        try:
            if args.lines is None:
                ln = default_random_spin_line_one()
                lines_list = [ln]
                print(f"[{_ts()}] Dòng quay mặc định: {ln} (random 1–20)", flush=True)
            else:
                lines_list = _parse_lines_arg(args.lines)
        except ValueError:
            print("❌ --lines không hợp lệ (chỉ số ngăn cách bằng dấu phẩy)")
            return
        spin = SpinOptions(bet_id=args.bet_id, lines=lines_list)

    profile = get_slot_profile(args.slot)
    lock = _OneUserOneSlotLock(username, profile.room_id)
    try:
        lock.acquire()
    except RuntimeError as e:
        print(f"❌ {e}", flush=True)
        return

    session_stats = SpinSessionStats()
    try:
        _asyncio_run_slot_main(
            connect_slot(
                username,
                profile,
                spin=spin,
                sync_slot_daily_db=not no_slot_daily,
                daily_mission=daily_mission,
                session_stats=session_stats,
                bet_cost_vnd=max(0, int(args.bet_cost_vnd)),
                stop_spin_if_balance_below=max(0, int(args.stop_below_balance)),
            )
        )
    except KeyboardInterrupt:
        print("\n⏹️ Đã dừng theo yêu cầu", flush=True)
    except OSError as e:
        # Windows: đóng WS đột ngột sau NV xong có thể khiến selector báo 10038 khi dọn event loop.
        if sys.platform == "win32" and getattr(e, "winerror", None) == 10038:
            pass
        else:
            raise
    finally:
        _print_spin_session_summary(username, session_stats, max(0, int(args.bet_cost_vnd)))
        lock.release()


if __name__ == "__main__":
    main()
