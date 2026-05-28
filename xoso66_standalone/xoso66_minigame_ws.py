# -*- coding: utf-8 -*-
"""
WebSocket mini-game XOSO66 (không dùng LC79 / Socket.IO).

  python xoso66_minigame_ws.py
  python xoso66_minigame_ws.py -u quangtyphu
  python xoso66_minigame_ws.py -a acc1 --duration 120

Giữ kết nối: subscribe + ping/pong. In jackpot, kết quả Tài/Xỉu, phiên mới (game_id catalog).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import random
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import socks

from xoso66_deposit import DEFAULT_UA
from xoso66_minigame_catalog import DEFAULT_JACKPOT_GAME_IDS, GAME_ID_LABELS
from xoso66_minigame_http import MINIGAME_BASE, get_minigame, ws_url_from_token

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

WS_HOST = urlparse(
    os.environ.get("XOSO66_MINIGAME_WS_BASE", "wss://wss-minigame-viet.227290.com")
).netloc or "wss-minigame-viet.227290.com"

# game_id trong subscribe — lobby 0; jackpot: 9,17,18,19,2 (bỏ game 4 — không hũ)
# Đủ 5 game: "0,9,[17,18,19,2]"
DEFAULT_WS_SUBSCRIBE = os.environ.get("XOSO66_WS_SUBSCRIBE", "0,9,[17,18,19,2]")
DEFAULT_WATCH_GAME_IDS = frozenset(DEFAULT_JACKPOT_GAME_IDS)
WS_PING_INTERVAL_SEC = float(os.environ.get("XOSO66_WS_PING_INTERVAL", "20"))

# Gợi ý nhận diện jackpot / pool trong payload JSON
_JACKPOT_TYPE_HINTS = frozenset(
    {
        "jackpot",
        "jackpots",
        "jackpot_update",
        "jackpot_money",
        "jackpot_pool",
        "pool",
        "prize_pool",
        "pool_update",
        "grand_pool",
    }
)
_JACKPOT_FIELD_HINTS = frozenset(
    {
        "jackpot",
        "jackpots",
        "jackpot_money",
        "jackpot_amount",
        "money",
        "pool",
        "pool_money",
        "prize_pool",
        "grand_prize",
        "total_jackpot",
    }
)


@dataclass
class JackpotState:
    """Jackpot theo game_id (cập nhật khi WS push)."""

    by_game: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_raw: list[dict[str, Any]] = field(default_factory=list)
    msg_count: int = 0
    jackpot_updates: int = 0


_ROUND_START_HANDLERS: list[Any] = []
_ROUND_RESULT_HANDLERS: list[Any] = []


def register_round_start_handler(
    fn: Any,
) -> None:
    """Đăng ký callback (game_id, issue, next_info, reporter=...) khi BẮT ĐẦU PHIÊN."""
    if fn not in _ROUND_START_HANDLERS:
        _ROUND_START_HANDLERS.append(fn)


def register_round_result_handler(
    fn: Any,
) -> None:
    """Callback (game_id, issue, open_data, reporter=...) khi KẾT QUẢ phiên."""
    if fn not in _ROUND_RESULT_HANDLERS:
        _ROUND_RESULT_HANDLERS.append(fn)


class WsBroadcastCoordinator:
    """
    Nhiều WS, một lần báo / lưu mỗi sự kiện — giống LC79 ``new-session`` + ``session_seen``:

    - Mọi acc giữ WS (dự phòng khi rớt).
    - Nick nào nhận gói trước ``claim`` được → xử lý + in log; nick sau im lặng.
    - Ghi ``minigame_jackpots.json`` / BẮT ĐẦU / KẾT QUẢ: cùng quy tắc claim (không phân nick chính/phụ).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        self._reporter: dict[str, str] = {}

    def _claim(self, key: str, reporter: str = "") -> bool:
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            if reporter:
                self._reporter[key] = reporter
            return True

    def reporter_of(self, key: str) -> str:
        with self._lock:
            return self._reporter.get(key) or ""

    def claim_jackpot(self, game_id: int, money: Any, *, reporter: str = "") -> bool:
        return self._claim(f"j:{int(game_id)}:{money}", reporter)

    def claim_round_start(self, game_id: int, issue: str, *, reporter: str = "") -> bool:
        issue = str(issue or "").strip()
        if not issue:
            return False
        return self._claim(f"s:{int(game_id)}:{issue}", reporter)

    def claim_round_result(self, game_id: int, issue: str, *, reporter: str = "") -> bool:
        issue = str(issue or "").strip()
        if not issue:
            return False
        return self._claim(f"r:{int(game_id)}:{issue}", reporter)

@dataclass
class MultiGameWatchState:
    """Theo dõi phiên / kết quả / hũ cho nhiều game_id."""

    watch_ids: frozenset[int]
    labels: dict[int, str] = field(default_factory=dict)
    last_issue: dict[int, str] = field(default_factory=dict)
    last_open: dict[int, str] = field(default_factory=dict)
    last_jackpot: dict[int, str] = field(default_factory=dict)
    msg_count: int = 0
    jackpot_store: Any = None
    ws_account: str = ""
    log_prefix: str = ""
    log_game_info: bool = False
    broadcast: WsBroadcastCoordinator | None = None
    focus_game_id: int | None = None


def parse_watch_game_ids(spec: str) -> frozenset[int]:
    s = (spec or "").strip().lower()
    if not s or s in ("all", "default"):
        return frozenset(DEFAULT_WATCH_GAME_IDS)
    if s in ("5", "6", "full", "jackpot"):
        return frozenset(DEFAULT_WATCH_GAME_IDS)
    out: set[int] = set()
    for part in s.replace("[", "").replace("]", "").split(","):
        p = part.strip()
        if p.isdigit():
            out.add(int(p))
    return frozenset(out) if out else frozenset(DEFAULT_WATCH_GAME_IDS)


def _game_tag(gid: int, labels: dict[int, str]) -> str:
    name = labels.get(gid) or GAME_ID_LABELS.get(gid) or ""
    return f"game_id={gid} ({name})" if name else f"game_id={gid}"


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _fmt_money(val: Any) -> str:
    s = str(val or "").strip()
    if not s:
        return "—"
    try:
        n = float(s.replace(",", ""))
        if n >= 1_000_000_000:
            return f"{n:,.0f}"
        return f"{n:,.2f}".rstrip("0").rstrip(".")
    except ValueError:
        return s


def resolve_account_id(*, username: str = "", account_id: str = "") -> str:
    """-u username hoặc -a acc id → account id trong DB."""
    aid = (account_id or "").strip()
    user = (username or "").strip()
    if aid:
        return aid
    if not user:
        return ""
    from xoso66_accounts_db import get_account_by_username

    row = get_account_by_username(user)
    if not row:
        raise SystemExit(f"Không tìm thấy account username='{user}' trong DB.")
    sess = row.get("session_json") or {}
    if not row.get("proxy") and not (sess.get("proxy") if isinstance(sess, dict) else False):
        raise SystemExit(f"Account '{user}' ({row['id']}) chưa có proxy trong DB.")
    return str(row["id"])


def _watch_gid(data: dict[str, Any]) -> int:
    return int(data.get("game_id") or data.get("id") or 0)


def _suppress_ws_watch_console() -> bool:
    """Ẩn KẾT QUẢ WS khi auto_bet tự in settlement (assign_bets_enabled)."""
    try:
        from xoso66_config_util import load_config

        ab = load_config().get("auto_bet")
        if not isinstance(ab, dict) or not ab.get("enabled"):
            return False
        return bool(ab.get("assign_bets_enabled", False))
    except Exception:
        return False


def _refresh_focus_game(state: MultiGameWatchState) -> int | None:
    """Game đang chơi (auto_bet) hoặc hũ cao nhất — lọc log BẮT ĐẦU PHIÊN / KẾT QUẢ."""
    try:
        from xoso66_config_util import load_config
        from xoso66_jackpot_picker import focus_game_id

        state.focus_game_id = focus_game_id(load_config())
    except Exception:
        state.focus_game_id = None
    return state.focus_game_id


def _focus_game_id_for_log(state: MultiGameWatchState) -> int | None:
    try:
        from xoso66_config_util import load_config
        from xoso66_jackpot_picker import focus_game_id

        return focus_game_id(load_config())
    except Exception:
        return state.focus_game_id


def _should_log_watch_game_focus(
    gid: int, state: MultiGameWatchState, *, kind: str = "result"
) -> bool:
    """Chỉ game đang chơi (auto_bet) hoặc hũ cao nhất."""
    if kind == "result" and _suppress_ws_watch_console():
        return False
    fid = _focus_game_id_for_log(state)
    if fid is None:
        return False
    return int(gid) == int(fid)


def _should_log_watch_game(gid: int, state: MultiGameWatchState) -> bool:
    """Chỉ game đang chơi (focus) — BẮT ĐẦU PHIÊN."""
    return _should_log_watch_game_focus(gid, state, kind="start")


def _should_log_watch_game_result(gid: int, state: MultiGameWatchState) -> bool:
    """Chỉ game đang chơi (focus) — KẾT QUẢ phiên."""
    return _should_log_watch_game_focus(gid, state, kind="result")


def _emit_round_result_log(gid: int, data: dict[str, Any], state: MultiGameWatchState) -> None:
    if not _should_log_watch_game_result(gid, state):
        return
    from xoso66_round_log import normalize_winning_side, winning_side_label

    res = data.get("open_result") or {}
    side = res.get("name") or res.get("result") or "?"
    nums = data.get("open_numbers") or res.get("open_numbers") or ""
    issue = str(data.get("issue") or "").strip()
    wside = normalize_winning_side(data)
    wlabel = winning_side_label(wside) if wside else str(side)
    name = _game_display_name(gid, state)
    nums_s = f" ({nums})" if nums else ""
    issue_s = f" issue={issue}" if issue else ""
    print(
        f"[{_ts()}] KẾT QUẢ PHIÊN - {name} - {wlabel}{nums_s}{issue_s}",
        flush=True,
    )


def _round_start_log_delay_sec() -> float:
    from xoso66_config_util import load_config

    cfg = load_config()
    gw = cfg.get("game_worker") if isinstance(cfg.get("game_worker"), dict) else {}
    ab = cfg.get("auto_bet") if isinstance(cfg.get("auto_bet"), dict) else {}
    if "round_start_log_delay_sec" in gw:
        return max(0.0, float(gw.get("round_start_log_delay_sec") or 0))
    if "round_start_log_delay_sec" in ab:
        return max(0.0, float(ab.get("round_start_log_delay_sec") or 0))
    return 0.0


def _jackpot_display_for_game(state: MultiGameWatchState, gid: int) -> str:
    store = state.jackpot_store
    if store is None:
        return "—"
    try:
        data = store.load()
        row = (data.get("by_game") or {}).get(str(int(gid))) or {}
        money = row.get("money")
        if money is not None:
            return _fmt_money(money)
    except Exception:
        pass
    return "—"


def _game_display_name(gid: int, state: MultiGameWatchState) -> str:
    return state.labels.get(gid) or GAME_ID_LABELS.get(gid) or f"game_id={gid}"


def _emit_round_start_log(
    gid: int, state: MultiGameWatchState, *, issue: str = ""
) -> None:
    if not _should_log_watch_game(gid, state):
        return
    # Auto-bet in BẮT ĐẦU PHIÊN sau bet_plan_after_sec (tránh trùng + đúng thứ tự sau KQ phiên trước).
    try:
        from xoso66_round_log import assign_bet_console_enabled

        if assign_bet_console_enabled():
            return
    except Exception:
        pass
    from xoso66_round_log import log_round_start_line

    name = _game_display_name(gid, state)
    jp_money = 0.0
    store = state.jackpot_store
    if store is not None:
        try:
            row = (store.load().get("by_game") or {}).get(str(int(gid))) or {}
            jp_money = float(str(row.get("money") or "0").replace(",", ""))
        except (TypeError, ValueError):
            jp_money = 0.0
    min_jp = 0.0
    try:
        from xoso66_config_util import load_config
        from xoso66_jackpot_picker import min_jackpot_vnd

        cfg = load_config()
        ab = cfg.get("auto_bet")
        if isinstance(ab, dict) and ab.get("enabled"):
            min_jp = min_jackpot_vnd(cfg)
    except Exception:
        pass
    log_round_start_line(
        game_label=name,
        jackpot_vnd=jp_money,
        issue=issue,
        min_jackpot_vnd=min_jp if min_jp > 0 else None,
    )


def _schedule_round_start_log(
    gid: int, state: MultiGameWatchState, *, issue: str = ""
) -> None:
    _refresh_focus_game(state)
    delay = _round_start_log_delay_sec()
    if delay <= 0:
        _emit_round_start_log(gid, state, issue=issue)
        return
    threading.Timer(
        delay,
        lambda g=gid, s=state, i=issue: _emit_round_start_log(g, s, issue=i),
    ).start()


def _print_watch_jackpot(data: dict[str, Any], state: MultiGameWatchState) -> bool:
    gid = _watch_gid(data)
    if gid not in state.watch_ids:
        return False
    money = data.get("money") if data.get("money") is not None else data.get("jackpot")
    try:
        from xoso66_jackpot_hit_notify import record_jackpot_pool

        record_jackpot_pool(gid, money)
    except Exception:
        pass
    key = f"{gid}:{money}"
    if state.last_jackpot.get(gid) == key:
        return False
    state.last_jackpot[gid] = key
    if state.broadcast is not None and not state.broadcast.claim_jackpot(
        gid, money, reporter=state.ws_account
    ):
        return True
    store = state.jackpot_store
    if store is not None:
        name = state.labels.get(gid) or GAME_ID_LABELS.get(gid) or ""
        changed = store.record(
            gid,
            money,
            game_name=name,
            ws_account=state.ws_account,
            group_id=data.get("group_id"),
        )
        _refresh_focus_game(state)
        if changed:
            try:
                from xoso66_config_util import load_config
                from xoso66_jackpot_picker import sync_auto_bet_jackpot_gate

                sync_auto_bet_jackpot_gate(load_config())
            except Exception:
                pass
    return True


def _print_watch_phiên_mới_from_next(
    gid: int, nxt: dict[str, Any], state: MultiGameWatchState
) -> bool:
    """Phiên mới = next_info trong open_info (~30s/phiên), không dùng game_info đổi issue."""
    issue = str(nxt.get("issue") or "")
    if not issue or state.last_issue.get(gid) == issue:
        return False
    state.last_issue[gid] = issue
    run_handlers = True
    if state.broadcast is not None:
        run_handlers = state.broadcast.claim_round_start(
            gid, issue, reporter=state.ws_account
        )
    if run_handlers:
        reporter = state.ws_account or (
            state.broadcast.reporter_of(f"s:{gid}:{issue}") if state.broadcast else ""
        )
        for handler in _ROUND_START_HANDLERS:
            try:
                handler(gid, issue, dict(nxt), reporter=reporter)
            except TypeError:
                handler(gid, issue, dict(nxt))
            except Exception as e:
                print(f"{state.log_prefix}[AUTO] round handler: {e}", flush=True)

    did_log = False
    if run_handlers and _should_log_watch_game(gid, state):
        _schedule_round_start_log(gid, state, issue=issue)
        did_log = True
    return run_handlers or did_log


def _print_watch_open_info(data: dict[str, Any], state: MultiGameWatchState) -> bool:
    gid = _watch_gid(data)
    if gid not in state.watch_ids:
        return False
    issue = str(data.get("issue") or "?")
    did_result = False
    if state.last_open.get(gid) != issue:
        state.last_open[gid] = issue
        run_handlers = True
        if state.broadcast is not None:
            run_handlers = state.broadcast.claim_round_result(
                gid, issue, reporter=state.ws_account
            )
        if run_handlers:
            reporter = state.ws_account or (
                state.broadcast.reporter_of(f"r:{gid}:{issue}")
                if state.broadcast
                else ""
            )
            for handler in _ROUND_RESULT_HANDLERS:
                try:
                    handler(gid, issue, dict(data), reporter=reporter)
                except TypeError:
                    handler(gid, issue, dict(data))
                except Exception as e:
                    print(f"{state.log_prefix}[AUTO] result handler: {e}", flush=True)
            _emit_round_result_log(gid, data, state)
            did_result = True
    nxt = data.get("next_info")
    did_new = False
    if isinstance(nxt, dict):
        did_new = _print_watch_phiên_mới_from_next(gid, nxt, state)
    return did_result or did_new


def _print_watch_game_info(data: dict[str, Any], state: MultiGameWatchState) -> bool:
    """game_info: chỉ log đếm ngược; PHIÊN MỚI lấy từ open_info→next_info."""
    gid = _watch_gid(data)
    if gid not in state.watch_ids:
        return False
    issue = str(data.get("issue") or "")
    open_flag = data.get("is_open")
    cd = data.get("countdown")
    status = "MỞ CƯỢC" if open_flag == 1 else "ĐÓNG CƯỢC"
    if (
        state.log_game_info
        and _should_log_watch_game(gid, state)
        and cd is not None
        and int(cd) in (30, 20, 15, 10, 5, 3, 1)
    ):
        print(
            f"{state.log_prefix}[{_ts()}] PHIÊN     {_game_tag(gid, state.labels)}  "
            f"issue={issue}  {status}  countdown={cd}s",
            flush=True,
        )
        return True
    return False


def handle_game_watch_message(
    obj: Any, state: MultiGameWatchState, *, debug_ws: bool = False
) -> bool:
    """In sự kiện phiên/jackpot; trả True nếu đã xử lý. Raise ConnectionError nếu logout."""
    if not isinstance(obj, dict):
        return False
    state.msg_count += 1
    t = str(obj.get("type") or "").lower()

    if t == "logout":
        print(f"{state.log_prefix}[{_ts()}] WS logout: {obj.get('msg')}", flush=True)
        raise ConnectionError(str(obj.get("msg") or "logout"))

    if t == "jackpot_money":
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        return _print_watch_jackpot(data, state)

    if t in ("g_open_info", "open_info"):
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        if debug_ws and _watch_gid(data) in state.watch_ids:
            import json

            print(
                f"[{_ts()}] DEBUG {t} {_game_tag(_watch_gid(data), state.labels)} "
                f"{json.dumps(data, ensure_ascii=False)[:300]}",
                flush=True,
            )
        return _print_watch_open_info(data, state)

    if t in ("g_game_info", "game_info"):
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        if debug_ws and _watch_gid(data) in state.watch_ids:
            import json

            print(
                f"[{_ts()}] DEBUG {t} {_game_tag(_watch_gid(data), state.labels)} "
                f"{json.dumps(data, ensure_ascii=False)[:300]}",
                flush=True,
            )
        return _print_watch_game_info(data, state)

    if t in ("balance", "g_balance"):
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        from xoso66_ws_balance import on_ws_balance_message, parse_ws_balance

        bal = parse_ws_balance(data)
        if bal is not None and state.ws_account:
            on_ws_balance_message(state.ws_account, bal)
        return True

    parsed = _parse_jackpot_money_message(obj)
    if parsed and _print_watch_jackpot(
        {"game_id": parsed.get("game_id"), "money": parsed.get("jackpot")},
        state,
    ):
        return True
    return False


WS_LOGOUT_FULL_REFRESH_COOLDOWN_SEC = float(
    os.environ.get("XOSO66_WS_FULL_REFRESH_COOLDOWN", "90")
)

_WS_TRANSPORT_ERR_HINTS = (
    "no close frame",
    "connection closed",
    "connection reset",
    "broken pipe",
    "eof",
    "timed out",
    "incomplete read",
)


def _is_transport_ws_error(err_s: str) -> bool:
    s = (err_s or "").lower()
    return any(h in s for h in _WS_TRANSPORT_ERR_HINTS)


def _clear_ws_token_cache(session: dict) -> None:
    """Xóa ws_token cache — bắt buộc lấy lại sau verification failed / subscribe lỗi."""
    mg = get_minigame(session)
    for key in ("ws_token", "ws_token_issued_at", "ws_url"):
        mg.pop(key, None)


def _cached_ws_token_if_ok(session: dict) -> str | None:
    from xoso66_minigame_refresh import _ws_token_age_ok, user_token_status

    mg = get_minigame(session)
    tok = mg.get("ws_token")
    if not tok or not _ws_token_age_ok(mg):
        return None
    st = user_token_status(session, do_ping=False)
    if not st.get("has_token") or st.get("needs_refresh"):
        return None
    return str(tok)

# Tránh vipList + nhận thưởng spam mỗi lần WS reconnect (cùng nick).
_WS_AFTER_CONNECT_VIP_LAST_TS: dict[str, float] = {}


async def _run_vip_check_after_ws_if_configured(
    account_id: str,
    username: str,
    *,
    game_watch: bool,
) -> None:
    """Sau WS OK + subscribe: check VIP + nhận thưởng (task nền)."""
    if not game_watch:
        return
    env_off = os.environ.get("XOSO66_WS_VIP_AFTER_CONNECT", "1").strip().lower()
    if env_off in ("0", "false", "no", "off"):
        return
    from xoso66_config_util import load_config

    cfg = load_config()
    gw = cfg.get("game_worker") if isinstance(cfg.get("game_worker"), dict) else {}
    if not gw.get("ws_vip_after_connect_enabled", True):
        return
    cooldown = max(0, int(gw.get("ws_vip_after_connect_cooldown_sec") or 3600))
    do_claim = bool(gw.get("ws_vip_after_connect_claim", True))
    aid = str(account_id or "").strip()
    if not aid:
        return

    now = time.time()
    if cooldown > 0:
        last = _WS_AFTER_CONNECT_VIP_LAST_TS.get(aid, 0.0)
        if now - last < cooldown:
            return
        _WS_AFTER_CONNECT_VIP_LAST_TS[aid] = now

    try:
        from xoso66_vip_check import vip_after_ws_connect

        r = await asyncio.to_thread(
            lambda: vip_after_ws_connect(aid, username, do_claim=do_claim)
        )
    except Exception as e:
        print(f"[VIP-WS] [{username}] {e}", flush=True)
        return

    ck = int(r.get("claims_ok") or 0)
    if ck > 0:
        print(f"[VIP-WS] [{username}] đã nhận {ck} thưởng VIP", flush=True)
        if gw.get("ws_vip_after_claim_refresh_balance", True):
            try:
                from xoso66_session import refresh_account_balance_to_db

                await asyncio.to_thread(
                    lambda: refresh_account_balance_to_db(aid, None, refresh=True)
                )
            except Exception as e:
                print(f"[VIP-WS] [{username}] refresh balance lỗi: {e}", flush=True)
    elif not r.get("ok"):
        err = str(r.get("error") or r.get("msg") or "?")
        print(f"[VIP-WS] [{username}] VIP lỗi: {err}", flush=True)


def _schedule_vip_after_ws_connect(
    account_id: str,
    username: str,
    *,
    game_watch: bool,
) -> None:
    """Tạo task nền — vòng recv chạy ngay."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    def _log_vip_task(task: asyncio.Task) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[VIP-WS] Task {task.get_name() or '?'} lỗi: {e}", flush=True)

    t = loop.create_task(
        _run_vip_check_after_ws_if_configured(
            account_id,
            username,
            game_watch=game_watch,
        ),
        name=f"vip-ws-{account_id}",
    )
    t.add_done_callback(_log_vip_task)


def _ws_logout_needs_full_refresh(msg: str | None) -> bool:
    """Server logout — cần refresh user-token + CF, không chỉ ws_token."""
    m = (msg or "").strip().lower()
    if not m:
        return False
    hints = (
        "verification",
        "verify",
        "auth",
        "token",
        "invalid",
        "expired",
        "unauthorized",
        "forbidden",
        "login",
        "session",
    )
    return any(h in m for h in hints)


def _ws_reconnect_flags_after_logout(msg: str | None) -> tuple[bool, bool]:
    """(need_minigame_refresh, need_ws_refresh)."""
    if _ws_logout_needs_full_refresh(msg):
        return True, False
    return False, True


async def _full_refresh_minigame_after_ws_logout(
    session: dict,
    account_id: str,
    *,
    username: str,
    game_key: str,
    last_refresh_at: list[float],
    force: bool = False,
) -> bool:
    """Refresh đầy đủ mini-game sau WS logout; có cooldown tránh Playwright liên tục."""
    from xoso66_minigame_refresh import refresh_minigame_tokens
    from xoso66_session import persist_session

    _clear_ws_token_cache(session)
    now = time.time()
    if (
        not force
        and last_refresh_at[0]
        and (now - last_refresh_at[0]) < WS_LOGOUT_FULL_REFRESH_COOLDOWN_SEC
    ):
        wait = WS_LOGOUT_FULL_REFRESH_COOLDOWN_SEC - (now - last_refresh_at[0])
        print(
            f"[{username}] WS logout — bỏ qua refresh full (cooldown còn {wait:.0f}s)",
            flush=True,
        )
        return False

    print(
        f"[{username}] WS logout — refresh full mini-game (user-token + ws + CF)…",
        flush=True,
    )
    last_refresh_at[0] = now
    rep = await asyncio.to_thread(
        refresh_minigame_tokens,
        session,
        account_id=account_id,
        game_key=game_key,
        force=True,
    )
    mg = rep.get("minigame") or {}
    if rep.get("ok") and mg.get("has_ws_token"):
        persist_session(account_id, session)
        print(f"[{username}] Refresh full mini-game OK", flush=True)
        return True

    err = rep.get("error") or (rep.get("ws_token") or {}).get("msg") or rep
    print(f"[{username}] Refresh full mini-game thất bại: {err}", flush=True)
    return False


def _build_socks(proxy_str: str) -> tuple[socks.socksocket, str, int]:
    from xoso66_proxy import parse_proxy

    host, port, user, pwd = parse_proxy(proxy_str)
    sock = socks.socksocket()
    sock.set_proxy(socks.SOCKS5, host, port, True, user, pwd)
    sock.setblocking(False)
    return sock, host, port


def _ws_timestamp_ms() -> int:
    return int(time.time() * 1000)


def _ws_unique_code(prefix: str) -> str:
    return f"{prefix}-{_ws_timestamp_ms()}-{random.randint(100, 999)}"


def parse_ws_subscribe_spec(spec: str) -> list[int | list[int]]:
    """
    Parse chuỗi subscribe: "0,9,[17,18,19,2]" → [0, 9, [17,18,19,2]].
    Mỗi phần = một frame subscribe gửi lên server.
    """
    out: list[int | list[int]] = []
    spec = (spec or "").strip()
    if not spec:
        return out
    i = 0
    while i < len(spec):
        if spec[i] == "[":
            end = spec.find("]", i)
            if end < 0:
                break
            inner = spec[i + 1 : end]
            ids = [int(x.strip()) for x in inner.split(",") if x.strip().isdigit()]
            if ids:
                out.append(ids)
            i = end + 1
            if i < len(spec) and spec[i] == ",":
                i += 1
            continue
        j = i
        while j < len(spec) and spec[j] not in ",[":
            j += 1
        token = spec[i:j].strip()
        if token.isdigit():
            out.append(int(token))
        i = j + 1 if j < len(spec) and spec[j] == "," else j
    return out


def build_ws_client_message(
    msg_type: str,
    *,
    game_id: int | list[int] | None = None,
    x_lang: str = "vi",
) -> str:
    """Frame JSON client gửi lên WS (subscribe / unsubscribe / ping)."""
    ts = _ws_timestamp_ms()
    if msg_type == "ping":
        body: dict[str, Any] = {
            "type": "ping",
            "unique_code": _ws_unique_code("ping"),
            "time": ts,
            "x-lang": x_lang,
            "data": game_id if game_id is not None else 0,
        }
    elif msg_type in ("subscribe", "unsubscribe"):
        body = {
            "type": msg_type,
            "time": ts,
            "x-lang": x_lang,
            "data": {
                "game_id": game_id if game_id is not None else 0,
                "unique_code": _ws_unique_code(msg_type),
            },
        }
    else:
        body = {"type": msg_type, "time": ts, "x-lang": x_lang}
    return json.dumps(body, ensure_ascii=False)


def flatten_subscribe_plan(
    plan: list[int | list[int]],
    *,
    extra_ids: frozenset[int] | None = None,
) -> list[int]:
    """Mỗi game_id một frame subscribe (batch [17,18,...] chủ yếu cho hũ)."""
    out: list[int] = []
    for item in plan:
        if isinstance(item, list):
            out.extend(int(x) for x in item)
        else:
            out.append(int(item))
    if extra_ids:
        for gid in sorted(extra_ids):
            if gid not in out:
                out.append(gid)
    seen: set[int] = set()
    unique: list[int] = []
    for gid in out:
        if gid not in seen:
            seen.add(gid)
            unique.append(gid)
    return unique


async def ws_send_subscribes(
    ws,
    subscribe_plan: list[int | list[int]],
    *,
    verbose: bool = True,
    individual: bool = False,
) -> None:
    ids = flatten_subscribe_plan(subscribe_plan) if individual else subscribe_plan
    if individual:
        for gid in ids:
            await ws.send(build_ws_client_message("subscribe", game_id=gid))
            if verbose:
                print(f"[WS] → subscribe game_id={gid}", flush=True)
            await asyncio.sleep(0.12)
        return
    for gid in subscribe_plan:
        msg = build_ws_client_message("subscribe", game_id=gid)
        await ws.send(msg)
        if verbose:
            print(f"[WS] → subscribe game_id={gid}", flush=True)
        await asyncio.sleep(0.12)


async def ws_ping_loop(
    ws,
    ping_game_id: int | list[int],
    *,
    interval_sec: float = WS_PING_INTERVAL_SEC,
    stop: asyncio.Event,
) -> None:
    ids = ping_game_id if isinstance(ping_game_id, list) else [int(ping_game_id)]
    if not ids:
        ids = [0]
    idx = 0
    while not stop.is_set():
        await asyncio.sleep(interval_sec)
        if stop.is_set():
            break
        gid = ids[idx % len(ids)]
        idx += 1
        try:
            await ws.send(build_ws_client_message("ping", game_id=gid))
        except Exception:
            break


def _decode_message(raw: str | bytes) -> Any:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return {"_binary_hex": raw[:200].hex()}
    text = (raw or "").strip()
    if not text:
        return None
    if text in ("ping", "PING"):
        return {"_app_ping": True}
    if text in ("pong", "PONG"):
        return {"_app_pong": True}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw_text": text[:500]}


def _is_ping_payload(obj: Any) -> bool:
    if obj == "ping":
        return True
    if not isinstance(obj, dict):
        return False
    t = str(obj.get("type") or obj.get("cmd") or obj.get("action") or "").lower()
    if t in ("ping", "heartbeat", "heart"):
        return True
    if obj.get("ping") is not None and not obj.get("pong"):
        return True
    return False


def _pong_reply(obj: Any) -> str | None:
    if obj == "ping":
        return "pong"
    if not isinstance(obj, dict):
        return None
    t = str(obj.get("type") or obj.get("cmd") or "").lower()
    if t in ("ping", "heartbeat", "heart"):
        out = dict(obj)
        out["type"] = "pong"
        if "cmd" in out:
            out["cmd"] = "pong"
        out.pop("ping", None)
        out["pong"] = out.get("time") or int(time.time())
        return json.dumps(out, ensure_ascii=False)
    if "ping" in obj:
        return json.dumps({"pong": obj.get("ping"), "time": int(time.time())}, ensure_ascii=False)
    return json.dumps({"type": "pong", "time": int(time.time())}, ensure_ascii=False)


def _parse_jackpot_money_message(obj: dict[str, Any]) -> dict[str, Any] | None:
    """WS type jackpot_money — data.game_id + data.money."""
    if str(obj.get("type") or "").lower() != "jackpot_money":
        return None
    data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
    gid = data.get("game_id")
    money = data.get("money")
    if gid is None and money is None:
        return None
    return {
        "game_id": gid,
        "jackpot": money,
        "group_id": data.get("group_id"),
        "source": "jackpot_money",
    }


def _normalize_jackpot_entry(item: dict[str, Any]) -> dict[str, Any] | None:
    parsed = _parse_jackpot_money_message(item)
    if parsed:
        return parsed
    gid = item.get("game_id") or item.get("gameId") or item.get("gid") or item.get("id")
    amount = None
    for k in _JACKPOT_FIELD_HINTS:
        if k in item and item[k] is not None:
            amount = item[k]
            break
    if amount is None and isinstance(item.get("data"), dict):
        data = item["data"]
        gid = gid or data.get("game_id") or data.get("gameId")
        for k in _JACKPOT_FIELD_HINTS:
            if k in data:
                amount = data[k]
                break
    name = item.get("game_name") or item.get("gameName") or item.get("name")
    if gid is None and amount is None:
        return None
    return {
        "game_id": gid,
        "game_name": name,
        "jackpot": amount,
        "raw_keys": [k for k in item.keys() if k in _JACKPOT_FIELD_HINTS or k in ("game_id", "gameId")],
    }


def _walk_extract_jackpots(obj: Any, found: list[dict[str, Any]]) -> None:
    if isinstance(obj, dict):
        t = str(obj.get("type") or obj.get("cmd") or obj.get("action") or "").lower()
        if t in _JACKPOT_TYPE_HINTS or any(k in obj for k in _JACKPOT_FIELD_HINTS):
            norm = _normalize_jackpot_entry(obj)
            if norm:
                found.append(norm)
            elif any(k in obj for k in _JACKPOT_FIELD_HINTS):
                found.append({"game_id": obj.get("game_id"), "jackpot": obj, "raw": True})

        for key in ("jackpots", "jackpot_list", "games", "list", "data", "items"):
            child = obj.get(key)
            if isinstance(child, list):
                for it in child:
                    if isinstance(it, dict):
                        n = _normalize_jackpot_entry(it)
                        if n:
                            found.append(n)
                        else:
                            _walk_extract_jackpots(it, found)
            elif isinstance(child, dict):
                _walk_extract_jackpots(child, found)

        for v in obj.values():
            if isinstance(v, (dict, list)) and v is not obj.get("data"):
                _walk_extract_jackpots(v, found)
    elif isinstance(obj, list):
        for it in obj:
            _walk_extract_jackpots(it, found)


def apply_jackpot_updates(state: JackpotState, found: list[dict[str, Any]]) -> bool:
    if not found:
        return False
    changed = False
    for entry in found:
        gid = str(entry.get("game_id") or "unknown")
        prev = state.by_game.get(gid)
        if prev != entry:
            state.by_game[gid] = entry
            changed = True
        state.last_raw.append(entry)
        if len(state.last_raw) > 50:
            state.last_raw.pop(0)
    if changed:
        state.jackpot_updates += 1
    return changed


def format_jackpot_table(state: JackpotState) -> str:
    if not state.by_game:
        return "(no jackpot yet)"
    lines = ["game_id | jackpot", "--------+--------"]
    for gid in sorted(state.by_game.keys(), key=lambda x: (x == "unknown", x)):
        row = state.by_game[gid]
        jp = row.get("jackpot")
        name = row.get("game_name") or ""
        extra = f" ({name})" if name else ""
        lines.append(f"{gid:7} | {jp}{extra}")
    return "\n".join(lines)


_ws_connect_sem: asyncio.Semaphore | None = None
_ws_connect_sem_limit = 0


async def _with_ws_connect_limit(coro):
    """Giới hạn số connect WS đồng thời (tránh 56 proxy cùng lúc)."""
    global _ws_connect_sem, _ws_connect_sem_limit
    from xoso66_config_util import load_config
    from xoso66_ws_pool import ws_connect_batch_size

    limit = ws_connect_batch_size(load_config())
    if _ws_connect_sem is None or _ws_connect_sem_limit != limit:
        _ws_connect_sem = asyncio.Semaphore(limit)
        _ws_connect_sem_limit = limit
    async with _ws_connect_sem:
        return await coro


async def _connect_ws(
    ws_url: str,
    proxy_str: str,
    *,
    origin: str = MINIGAME_BASE,
    ping_interval: float = 25.0,
    ping_timeout: float = 20.0,
):
    import websockets

    sock, _ph, _pp = _build_socks(proxy_str)
    try:
        sock.connect((WS_HOST, 443))
    except Exception as e:
        raise ConnectionError(f"SOCKS connect {WS_HOST}:443 failed: {e}") from e

    try:
        # Chỉ dùng ping JSON app (ws_ping_loop); tránh ping kép gây đứt socket.
        ws = await websockets.connect(
            ws_url,
            sock=sock,
            ssl=True,
            ping_interval=None,
            ping_timeout=None,
            close_timeout=5,
            origin=origin,
            user_agent_header=os.environ.get("XOSO66_WS_UA", DEFAULT_UA),
            max_size=2**22,
        )
    except Exception:
        sock.close()
        raise
    return ws, sock


async def listen_minigame_ws(
    session: dict,
    account_id: str,
    *,
    duration_sec: float = 0,
    game_key: str = "taixiu_dai_loc",
    refresh_before_connect: bool = True,
    ws_token_override: str | None = None,
    verbose: bool = True,
    game_watch: bool = True,
    watch_game_ids: frozenset[int] | None = None,
    subscribe_spec: str | None = None,
    ping_game_id: int | None = None,
    debug_ws: bool = False,
    subscribe_individual: bool = False,
    save_jackpot: bool = True,
    jackpot_store: Any = None,
    log_game_info: bool = False,
    broadcast_coordinator: WsBroadcastCoordinator | None = None,
) -> JackpotState:
    """
    Kết nối WS, giữ ping, in jackpot + phiên/kết quả. duration_sec=0 → chạy đến Ctrl+C.
    """
    from xoso66_minigame_session import get_ws_token
    from xoso66_minigame_catalog import game_by_key
    from xoso66_minigame_refresh import prep_tokens_before_ws
    from xoso66_proxy import ensure_proxy
    from xoso66_session import ensure_session, prep_site_session_before_ws

    g = game_by_key(game_key)
    primary_gid = int(g["game_id"])
    subscribe_plan = parse_ws_subscribe_spec(subscribe_spec or DEFAULT_WS_SUBSCRIBE)
    if not subscribe_plan:
        subscribe_plan = [0, primary_gid]
    watch_ids = watch_game_ids if watch_game_ids is not None else frozenset(DEFAULT_WATCH_GAME_IDS)
    multi_watch = bool(game_watch and len(watch_ids) > 1)
    solo_watch_gid = next(iter(watch_ids)) if game_watch and len(watch_ids) == 1 else None
    ping_gid: int | list[int]
    if ping_game_id is not None:
        if isinstance(ping_game_id, (list, tuple, frozenset, set)):
            ping_gid = [int(x) for x in ping_game_id]
        else:
            ping_gid = int(ping_game_id)
    elif solo_watch_gid is not None:
        ping_gid = int(solo_watch_gid)
    elif multi_watch:
        ping_gid = sorted(watch_ids)
    else:
        ping_gid = primary_gid

    from xoso66_accounts_db import username_for_log

    aid = account_id or str(session.get("id") or "")
    await asyncio.to_thread(prep_site_session_before_ws, aid)
    session = await asyncio.to_thread(ensure_session, aid, force_login=False)
    await asyncio.to_thread(ensure_proxy, session)

    username = username_for_log(aid, session)

    if not ws_token_override:
        rep = await asyncio.to_thread(
            prep_tokens_before_ws,
            session,
            aid,
            game_key=game_key,
            force_ws=bool(refresh_before_connect),
        )
        if not rep.get("user_token_ok"):
            err = rep.get("error") or "user-token không dùng được trước WS"
            raise RuntimeError(f"Không mở WS — {err}")
        if not rep.get("ok") or not rep.get("has_ws_token"):
            ws_err = rep.get("ws_token") or {}
            err = (
                rep.get("error")
                or ws_err.get("msg")
                or ws_err.get("error")
                or "không lấy được ws_token"
            )
            raise RuntimeError(f"Không mở WS — {err}")
        if game_watch:
            from xoso66_round_log import log_ws_connecting

            log_ws_connecting(username, user_token_ok=True)

    proxy_str = session["proxy"]

    jp_store = None
    if save_jackpot:
        if jackpot_store is not None:
            jp_store = jackpot_store
        else:
            from xoso66_minigame_jackpot_store import MinigameJackpotStore

            jp_store = MinigameJackpotStore()

    state = JackpotState()
    log_prefix = f"[{username}] " if game_watch else ""
    watch = (
        MultiGameWatchState(
            watch_ids=watch_ids,
            labels=dict(GAME_ID_LABELS),
            jackpot_store=jp_store,
            ws_account=aid,
            log_prefix=log_prefix,
            log_game_info=log_game_info,
            broadcast=broadcast_coordinator,
        )
        if game_watch
        else None
    )
    if watch is not None and jp_store is not None:
        _refresh_focus_game(watch)
        if watch.focus_game_id is None:
            from xoso66_config_util import load_config, main_progress

            ab = load_config().get("auto_bet")
            min_jp = (
                float(ab.get("min_jackpot_vnd") or 0)
                if isinstance(ab, dict)
                else 0.0
            )
            if min_jp > 0:
                main_progress(
                    f"[{username}] WS OK — lưu hũ 5 game; "
                    f"log phiên khi có game ≥ {min_jp:,.0f}đ"
                )
    deadline = time.time() + duration_sec if duration_sec > 0 else None
    reconnect_delay = 5.0 if game_watch else 3.0
    manual_ws_token = bool(ws_token_override)
    need_ws_refresh = False
    need_minigame_refresh = False
    last_full_refresh_at: list[float] = [0.0]
    verify_fail_streak = 0
    had_live_ws = False
    transport_fail_streak = 0
    force_fresh_tokens = bool(refresh_before_connect)

    from xoso66_shutdown import stopping

    while True:
        if stopping():
            break
        if deadline and time.time() >= deadline:
            break

        if need_minigame_refresh:
            need_minigame_refresh = False
            refreshed = await _full_refresh_minigame_after_ws_logout(
                session,
                aid,
                username=username,
                game_key=game_key,
                last_refresh_at=last_full_refresh_at,
                force=force_fresh_tokens or verify_fail_streak > 0,
            )
            session = await asyncio.to_thread(ensure_session, aid, force_login=False)
            await asyncio.to_thread(ensure_proxy, session)
            proxy_str = session["proxy"]
            if not refreshed:
                rep = await asyncio.to_thread(
                    refresh_minigame_tokens,
                    session,
                    account_id=aid,
                    game_key=game_key,
                    force=True,
                    ws_only=False,
                )
                session = await asyncio.to_thread(ensure_session, aid, force_login=False)
                refreshed = bool(rep.get("ok") and (rep.get("minigame") or {}).get("has_ws_token"))
                if not refreshed:
                    need_ws_refresh = True

        try:
            if ws_token_override and manual_ws_token:
                token = ws_token_override.strip()
                mg = get_minigame(session)
                mg["ws_token"] = token
                mg["ws_url"] = ws_url_from_token(token)
            else:
                skip_cache = (
                    force_fresh_tokens
                    or need_ws_refresh
                    or need_minigame_refresh
                    or verify_fail_streak > 0
                )
                cached = None if skip_cache else _cached_ws_token_if_ok(session)
                if cached:
                    token = cached
                else:
                    if had_live_ws and game_watch:
                        print(
                            f"🔐 [{username}] đang lấy lại ws_token…",
                            flush=True,
                        )
                    token = await asyncio.to_thread(
                        get_ws_token,
                        session,
                        aid,
                        game_key=game_key,
                        force_refresh=need_ws_refresh,
                    )
                need_ws_refresh = False
                manual_ws_token = False
                ws_token_override = None
        except Exception as e:
            need_ws_refresh = True
            print(f"[WS] [{username}] ws_token failed: {e}", flush=True)
            if deadline:
                await asyncio.sleep(reconnect_delay)
                continue
            raise

        ws_url = ws_url_from_token(token)

        ws = None
        sock = None
        ping_stop = asyncio.Event()
        ping_task: asyncio.Task | None = None
        try:
            ws, sock = await _with_ws_connect_limit(_connect_ws(ws_url, proxy_str))
            ping_task = asyncio.create_task(
                ws_ping_loop(ws, ping_gid, stop=ping_stop)
            )
            try:
                await ws_send_subscribes(
                    ws,
                    subscribe_plan,
                    verbose=verbose,
                    individual=subscribe_individual,
                )
            except Exception as e:
                print(f"[WS] subscribe failed: {e}", flush=True)
                _clear_ws_token_cache(session)
                need_ws_refresh = True
                force_fresh_tokens = True
            if game_watch:
                from xoso66_round_log import log_ws_connected
                from xoso66_ws_pool import register_ws_connected

                register_ws_connected(aid)
                first_live = not had_live_ws
                if first_live:
                    log_ws_connected(username, account_id=aid)
                    _schedule_vip_after_ws_connect(
                        aid, username, game_watch=game_watch
                    )
                had_live_ws = True
                transport_fail_streak = 0
            if verbose and not game_watch:
                print(
                    f"[WS] {username} connected subscribe={subscribe_plan} ping={ping_gid}",
                    flush=True,
                )

            while True:
                if stopping():
                    break
                if deadline and time.time() >= deadline:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=45.0)
                except asyncio.TimeoutError:
                    if stopping():
                        break
                    if verbose:
                        print("[WS] recv timeout (45s) — keep-alive", flush=True)
                    continue

                state.msg_count += 1
                obj = _decode_message(raw)

                if watch is not None and handle_game_watch_message(
                    obj, watch, debug_ws=debug_ws
                ):
                    continue

                if isinstance(obj, dict) and str(obj.get("type") or "").lower() == "logout":
                    logout_msg = str(obj.get("msg") or "")
                    full, ws_only = _ws_reconnect_flags_after_logout(logout_msg)
                    action = (
                        "refresh full mini-game"
                        if full
                        else "refresh ws token"
                    )
                    print(
                        f"[WS] logout: {logout_msg or '?'} — {action}",
                        flush=True,
                    )
                    if "verification" in logout_msg.lower():
                        verify_fail_streak += 1
                        force_fresh_tokens = True
                        _clear_ws_token_cache(session)
                        reconnect_delay = min(
                            45.0,
                            reconnect_delay * (1.0 + 0.5 * verify_fail_streak),
                        )
                    else:
                        verify_fail_streak = 0
                    need_minigame_refresh = need_minigame_refresh or full
                    need_ws_refresh = need_ws_refresh or ws_only
                    if "verification" in logout_msg.lower():
                        need_minigame_refresh = True
                        need_ws_refresh = False
                    manual_ws_token = False
                    ws_token_override = None
                    break

                if watch is not None:
                    continue

                if isinstance(obj, dict) and str(obj.get("type") or "").lower() in (
                    "jackpot",
                    "jackpots",
                    "jackpot_update",
                    "jackpot_money",
                    "pool",
                    "pool_update",
                ):
                    found: list[dict[str, Any]] = []
                    _walk_extract_jackpots(obj, found)
                    if not found:
                        found.append(
                            {
                                "game_id": obj.get("game_id"),
                                "jackpot": obj.get("data") or obj,
                                "raw": True,
                            }
                        )
                    if apply_jackpot_updates(state, found):
                        print(
                            f"\n[JACKPOT] #{state.jackpot_updates} @ {time.strftime('%H:%M:%S')}\n"
                            f"{format_jackpot_table(state)}\n",
                            flush=True,
                        )
                    continue

                if isinstance(obj, dict) and obj.get("_app_ping"):
                    await ws.send("pong")
                    continue

                if _is_ping_payload(obj):
                    reply = _pong_reply(obj)
                    if reply:
                        await ws.send(reply)
                    continue

                found: list[dict[str, Any]] = []
                _walk_extract_jackpots(obj, found)
                if apply_jackpot_updates(state, found):
                    print(
                        f"\n[JACKPOT] #{state.jackpot_updates} @ {time.strftime('%H:%M:%S')}\n"
                        f"{format_jackpot_table(state)}\n",
                        flush=True,
                    )
                elif verbose and state.msg_count <= 15:
                    preview = raw if isinstance(raw, str) else str(raw)[:300]
                    print(f"[WS] msg#{state.msg_count}: {preview[:280]}", flush=True)
                elif verbose and state.msg_count == 16:
                    print("[WS] (suppress raw log — jackpot only)", flush=True)

        except asyncio.CancelledError:
            raise
        except ConnectionError as e:
            err_s = str(e)
            full, ws_only = _ws_reconnect_flags_after_logout(err_s)
            if "verification" in err_s.lower():
                verify_fail_streak += 1
                force_fresh_tokens = True
                _clear_ws_token_cache(session)
                reconnect_delay = min(
                    45.0,
                    reconnect_delay * (1.0 + 0.5 * verify_fail_streak),
                )
            else:
                verify_fail_streak = 0
            need_minigame_refresh = need_minigame_refresh or full
            need_ws_refresh = need_ws_refresh or ws_only
            if "verification" in err_s.lower():
                need_minigame_refresh = True
                need_ws_refresh = False
            print(
                f"[{_ts()}] Mất kết nối: {e} — đóng WS, reconnect sau {reconnect_delay}s",
                flush=True,
            )
            break
        except Exception as e:
            err_s = str(e).lower()
            if _is_transport_ws_error(err_s):
                transport_fail_streak += 1
                need_ws_refresh = False
                need_minigame_refresh = False
                reconnect_delay = min(
                    30.0,
                    reconnect_delay + min(10.0, 2.0 * transport_fail_streak),
                )
                if game_watch and had_live_ws:
                    print(
                        f"[WS] [{username}] mất kết nối tạm ({e}) — "
                        f"giữ ws_token, thử lại sau {reconnect_delay:.0f}s",
                        flush=True,
                    )
                    from xoso66_minigame_refresh import (
                        request_urgent_token_refresh,
                        user_token_status,
                    )

                    gname_drop = str(g.get("gamename") or "lobby")
                    st_drop = await asyncio.to_thread(
                        user_token_status,
                        session,
                        game_id=primary_gid,
                        gamename=gname_drop,
                    )
                    if st_drop.get("ping_ok") is False:
                        need_minigame_refresh = True
                        request_urgent_token_refresh(aid, game_key=game_key)
                else:
                    print(
                        f"[WS] Error: {e} — reconnect in {reconnect_delay}s",
                        flush=True,
                    )
            else:
                if _ws_logout_needs_full_refresh(err_s):
                    need_minigame_refresh = True
                elif "ws_token" in err_s or "user-token" in err_s:
                    need_ws_refresh = True
                print(f"[WS] Error: {e} — reconnect in {reconnect_delay}s", flush=True)
            break
        finally:
            if game_watch:
                from xoso66_ws_pool import unregister_ws_connected

                unregister_ws_connected(aid)
            ping_stop.set()
            if ping_task is not None:
                ping_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await ping_task
            if ws is not None:
                with contextlib.suppress(Exception):
                    await ws.close()
            if sock is not None:
                with contextlib.suppress(Exception):
                    sock.close()

        if deadline and time.time() >= deadline:
            break
        if stopping():
            break
        if duration_sec <= 0:
            if not had_live_ws:
                print(f"[WS] Closed — reconnect in {reconnect_delay}s", flush=True)
            for _ in range(int(reconnect_delay)):
                if stopping():
                    break
                await asyncio.sleep(1)
        else:
            break

    return state


async def _amain(args: argparse.Namespace) -> int:
    account_id = resolve_account_id(username=args.username, account_id=args.account)
    ws_tok = (args.ws_token or os.environ.get("XOSO66_WS_TOKEN") or "").strip() or None
    watch_ids = parse_watch_game_ids(args.watch_games)
    state = await listen_minigame_ws(
        {},
        account_id,
        duration_sec=args.duration,
        game_key=args.game,
        refresh_before_connect=not args.ws_only and not ws_tok,
        ws_token_override=ws_tok,
        verbose=not args.quiet,
        game_watch=not args.jackpot_table,
        watch_game_ids=watch_ids,
        subscribe_spec=args.subscribe or None,
        ping_game_id=args.ping_game_id,
        debug_ws=args.debug_ws,
        subscribe_individual=args.subscribe_individual,
        save_jackpot=not args.no_save_jackpot,
    )
    print(
        f"\n[WS] Total: messages={state.msg_count}, jackpot_updates={state.jackpot_updates}, "
        f"games={len(state.by_game)}",
        flush=True,
    )
    if state.by_game:
        print(format_jackpot_table(state), flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="XOSO66 mini-game WebSocket")
    ap.add_argument("-u", "--username", default="", help="username trong DB")
    ap.add_argument("-a", "--account", default="", help="account id (acc1) — thay cho -u")
    ap.add_argument("--duration", type=float, default=0, help="giây chạy (0 = đến Ctrl+C)")
    ap.add_argument("--game", default="taixiu_dai_loc")
    ap.add_argument("--ws-only", action="store_true", help="không refresh user-token, chỉ getToken")
    ap.add_argument(
        "--jackpot-table",
        action="store_true",
        help="chế độ cũ: in bảng jackpot thay vì KẾT QUẢ/PHIÊN MỚI",
    )
    ap.add_argument("--quiet", action="store_true", help="ít log raw (với --jackpot-table)")
    ap.add_argument("--ws-token", default="", help="bỏ qua getToken — dùng token WS tay")
    ap.add_argument(
        "--watch-games",
        default="all",
        help='game_id theo dõi, VD: "9" (mặc định debug), "6" hoặc all = 6 game',
    )
    ap.add_argument(
        "--subscribe",
        default="",
        help='subscribe sau connect, VD: "0,9,[17,18,19,2]"',
    )
    ap.add_argument(
        "--ping-game-id",
        type=int,
        default=None,
        help="ping cố định 1 game_id (thử: 17)",
    )
    ap.add_argument(
        "--debug-ws",
        action="store_true",
        help="in raw g_game_info / g_open_info (thử 1 game lạ)",
    )
    ap.add_argument(
        "--subscribe-individual",
        action="store_true",
        help="subscribe từng game_id (mặc định: 0 + [9,17,18,19,2] — 5 game có hũ)",
    )
    ap.add_argument(
        "--no-save-jackpot",
        action="store_true",
        help="không ghi file minigame_jackpots.json",
    )
    args = ap.parse_args()
    if not (args.username or "").strip() and not (args.account or "").strip():
        from xoso66_config_util import ws_default_username

        default_u = ws_default_username()
        if default_u:
            args.username = default_u
            print(f"[WS] Username mặc định: {default_u}", flush=True)
        else:
            args.username = input("Username: ").strip()
    if not resolve_account_id(username=args.username, account_id=args.account):
        print("Cần -u username hoặc -a account.", file=sys.stderr)
        return 1
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        print(f"\n[{_ts()}] Dừng.", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
