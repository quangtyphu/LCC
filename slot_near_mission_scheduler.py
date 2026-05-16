"""
Chế độ quay slot ưu tiên NV (symbol gần 600): định kỳ lấy user Đang Chơi / Hết Tiền,
chọn top N theo max(symbol_count slot 39, 40), nạp nếu số dư < ngưỡng, rồi chạy
ws_slot_client.py lần lượt --slot 39 rồi 40.
User số dư < ``BALANCE_MIN_BEFORE_SPIN`` khi chọn: tạm loại ``BALANCE_LOW_SKIP_COOLDOWN_SECONDS``
khỏi vòng ưu tiên để user khác được thay (hết hạn hoặc đủ số dư thì vào lại).

Bật: config.json → SLOT_NEAR_MISSION_SPIN.ENABLED = 1
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Tuple

import requests

from constants import load_config
from ws_slot_client import default_random_spin_line_one, user_slot_client_lock_busy

API_BASE = "http://127.0.0.1:3000"
FETCH_URL = f"{API_BASE}/api/users/lc79-playing-or-out"
STATS_URL = f"{API_BASE}/api/slot-game-daily/stats"

_ALLOWED_STATUSES = frozenset({"Đang Chơi", "Hết Tiền"})

_cycle_lock = threading.Lock()

# Thoát main (Ctrl+C): WS tài xỉu bị hủy task; slot NV spawn subprocess — Windows thường không gửi SIGINT cho con.
_slot_nv_shutdown_event = threading.Event()
_slot_child_lock = threading.Lock()
_slot_child_procs: List[subprocess.Popen] = []
_slot_children_terminate_done = False
_slot_children_terminate_lock = threading.Lock()

# User vừa bị bỏ qua vì số dư < BALANCE_MIN_BEFORE_SPIN (chờ nạp / cache treo): tạm không vào scored
# để user khác được thay thế; hết hạn hoặc số dư đã ≥ ngưỡng thì gỡ (xem _slot_nv_*_balance_skip).
_slot_nv_balance_skip_until: Dict[str, float] = {}
_slot_nv_balance_skip_lock = threading.Lock()


def _to_int(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _parse_users_payload(data: Any) -> List[dict]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("data", "users", "items"):
            inner = data.get(key)
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
    return []


def _username_from_row(row: dict) -> str:
    u = row.get("username") or row.get("user") or row.get("name")
    return str(u).strip() if u else ""


def _fetch_playing_or_out_usernames() -> List[str]:
    try:
        r = requests.get(FETCH_URL, timeout=15)
    except Exception as e:
        print(f"[SLOT_NV] ⚠️ Lỗi gọi {FETCH_URL}: {e}", flush=True)
        return []
    if r.status_code != 200:
        print(f"[SLOT_NV] ⚠️ API {r.status_code}: {r.text[:200]}", flush=True)
        return []
    try:
        payload = r.json()
    except Exception as e:
        print(f"[SLOT_NV] ⚠️ Parse JSON: {e}", flush=True)
        return []
    rows = _parse_users_payload(payload)
    seen: Dict[str, bool] = {}
    out: List[str] = []
    for row in rows:
        st = str(row.get("status") or "").strip()
        if st not in _ALLOWED_STATUSES:
            continue
        u = _username_from_row(row)
        if not u or u.lower() in seen:
            continue
        seen[u.lower()] = True
        out.append(u)
    return out


def _get_stats(username: str, slot_id: int) -> dict | None:
    try:
        r = requests.get(
            STATS_URL,
            params={"username": username, "slot_id": int(slot_id)},
            timeout=8,
        )
        if r.status_code != 200:
            return None
        return r.json() if isinstance(r.json(), dict) else None
    except Exception:
        return None


def _stats_slice(body: dict | None) -> Tuple[int, int, bool]:
    """symbol_count (theo ngày VN server), reward_claimed, same_day."""
    if not body or not body.get("ok"):
        return 0, 0, False
    same = bool(body.get("same_day"))
    sym = _to_int(body.get("symbol_count"), 0)
    rc = _to_int(body.get("reward_claimed"), 0)
    return sym, rc, same


def _user_skip_both_slots_done(s39: dict | None, s40: dict | None) -> bool:
    """Đã nhận thưởng NV cả hai bàn trong cùng ngày VN (theo JSON stats)."""
    _, r39, d39 = _stats_slice(s39)
    _, r40, d40 = _stats_slice(s40)
    if d39 and r39 == 1 and d40 and r40 == 1:
        return True
    return False


def _slot_needs_nv_spin(sym: int, rc: int, same: bool, symbol_target: int) -> bool:
    """Còn cần phiên quay NV trên bàn này (theo stats cùng ngày)."""
    if not same:
        return True
    if rc == 1:
        return False
    return sym < symbol_target


def _user_needs_nv_any_slot(
    stats_by_sid: Dict[int, dict | None],
    slots: List[int],
    symbol_target: int,
) -> bool:
    """True nếu ít nhất một bàn trong SLOT_ORDER vẫn cần quay (chưa reward, symbol < mốc khi same_day)."""
    for sid in slots:
        sym, rc, same = _stats_slice(stats_by_sid.get(sid))
        if _slot_needs_nv_spin(sym, rc, same, symbol_target):
            return True
    return False


def _max_symbol_from_pair(s39: dict | None, s40: dict | None) -> int:
    a, _, sa = _stats_slice(s39)
    b, _, sb = _stats_slice(s40)
    return max(a if sa else 0, b if sb else 0)


def _user_in_minigame_ws(username: str) -> bool:
    try:
        from constants import active_ws

        return username in (active_ws or {})
    except Exception:
        return False


def _slot_nv_user_key(username: str) -> str:
    return str(username or "").strip().lower()


def _slot_nv_balance_skip_active(username: str) -> bool:
    k = _slot_nv_user_key(username)
    if not k:
        return False
    now = time.monotonic()
    with _slot_nv_balance_skip_lock:
        until = _slot_nv_balance_skip_until.get(k)
        if until is None:
            return False
        if now >= until:
            del _slot_nv_balance_skip_until[k]
            return False
        return True


def _slot_nv_set_balance_skip(username: str, cooldown_sec: int) -> None:
    k = _slot_nv_user_key(username)
    if not k:
        return
    until = time.monotonic() + max(15, int(cooldown_sec))
    with _slot_nv_balance_skip_lock:
        _slot_nv_balance_skip_until[k] = until


def _slot_nv_clear_balance_skip_if_recovered(username: str, min_balance: int) -> None:
    """Đủ số dư → gỡ cooldown để user vào lại vòng chọn NV."""
    k = _slot_nv_user_key(username)
    if not k:
        return
    from get_balance import get_balance

    gb = get_balance(username, force=True)
    if not gb.get("ok"):
        return
    if _to_int(gb.get("balance"), 0) < min_balance:
        return
    with _slot_nv_balance_skip_lock:
        _slot_nv_balance_skip_until.pop(k, None)


def note_slot_nv_balance_skip_from_ws_client(username: str) -> None:
    """
    Gọi từ tiến trình main (HTTP) khi ws_slot_client báo hết tiền:
    loại user khỏi vòng ưu tiên SLOT_NV theo ``BALANCE_LOW_SKIP_COOLDOWN_SECONDS``.
    """
    cfg = load_config()
    block = cfg.get("SLOT_NEAR_MISSION_SPIN")
    if not isinstance(block, dict):
        block = {}
    skip_cd = max(15, _to_int(block.get("BALANCE_LOW_SKIP_COOLDOWN_SECONDS", 120), 120))
    _slot_nv_set_balance_skip(username, skip_cd)
    print(
        f"[SLOT_NV] ⏸️ [{username}] Loại khỏi ưu tiên NV {skip_cd}s (hết tiền — ws_slot).",
        flush=True,
    )


def _ensure_balance_min(
    username: str,
    min_balance: int,
    poll_max_sec: int,
    balance_skip_cooldown_sec: int = 120,
) -> bool:
    from get_balance import get_balance

    from auto_deposit_on_out_of_money import (
        can_create_deposit_order,
        enqueue_deposit_order,
    )

    gb = get_balance(username, force=True)
    if not gb.get("ok"):
        print(f"[SLOT_NV] ⚠️ [{username}] get_balance thất bại: {gb.get('error')}", flush=True)
        _slot_nv_set_balance_skip(username, balance_skip_cooldown_sec)
        return False
    bal = _to_int(gb.get("balance"), 0)
    if bal >= min_balance:
        _slot_nv_clear_balance_skip_if_recovered(username, min_balance)
        return True
    if not can_create_deposit_order(username):
        print(
            f"[SLOT_NV] ⚠️ [{username}] Số dư {bal} < {min_balance}, không tạo nạp (cache/treo).",
            flush=True,
        )
        _slot_nv_set_balance_skip(username, balance_skip_cooldown_sec)
        return False
    enqueue_deposit_order(
        username,
        f"[SLOT_NV] số dư {bal}đ < {min_balance}đ trước quay slot",
    )
    deadline = time.monotonic() + max(30, int(poll_max_sec))
    while time.monotonic() < deadline:
        if _slot_nv_shutdown_event.is_set():
            return False
        time.sleep(8)
        gb2 = get_balance(username, force=True)
        if gb2.get("ok") and _to_int(gb2.get("balance"), 0) >= min_balance:
            _slot_nv_clear_balance_skip_if_recovered(username, min_balance)
            return True
    print(f"[SLOT_NV] ⚠️ [{username}] Chờ nạp quá {poll_max_sec}s, vẫn < {min_balance}.", flush=True)
    _slot_nv_set_balance_skip(username, balance_skip_cooldown_sec)
    return False


def request_slot_near_mission_scheduler_stop() -> None:
    """Đánh dấu dừng scheduler + không spawn thêm ws_slot_client trong tick hiện tại."""
    _slot_nv_shutdown_event.set()


def terminate_slot_near_mission_subprocesses() -> None:
    """
    Kết thúc mọi tiến trình con ``ws_slot_client`` do SLOT_NV đang chạy.
    Gọi từ main khi Ctrl+C / thoát — tránh con tiếp tục quay khi parent đã dừng (đặc biệt Windows).
    """
    global _slot_children_terminate_done
    with _slot_children_terminate_lock:
        if _slot_children_terminate_done:
            return
        _slot_children_terminate_done = True
    request_slot_near_mission_scheduler_stop()
    with _slot_child_lock:
        pending = list(_slot_child_procs)
    for p in pending:
        if p.poll() is None:
            with contextlib.suppress(Exception):
                p.terminate()
    for p in pending:
        with contextlib.suppress(Exception):
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(Exception):
                    p.kill()
    with _slot_child_lock:
        _slot_child_procs.clear()


def _run_ws_slot_client(
    username: str,
    slot_id: int,
    subprocess_timeout: int | None,
) -> int:
    root = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(root, "ws_slot_client.py")
    cmd = [
        sys.executable,
        script,
        username,
        "--slot",
        str(int(slot_id)),
        "--bet-id",
        "0",
        "--lines",
        str(default_random_spin_line_one()),
    ]
    kw: dict = {"cwd": root}
    timeout_s: int | None = None
    if subprocess_timeout and subprocess_timeout > 0:
        timeout_s = int(subprocess_timeout)
    p = subprocess.Popen(cmd, **kw)
    with _slot_child_lock:
        _slot_child_procs.append(p)
    try:
        if timeout_s is not None:
            try:
                p.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                print(
                    f"[SLOT_NV] ⏱️ [{username}] --slot {slot_id} hết timeout subprocess.",
                    flush=True,
                )
                with contextlib.suppress(Exception):
                    p.terminate()
                with contextlib.suppress(Exception):
                    p.wait(timeout=8)
                return -9
        else:
            p.wait()
        return int(p.returncode or 0)
    finally:
        with _slot_child_lock:
            with contextlib.suppress(ValueError):
                _slot_child_procs.remove(p)


def slot_near_mission_tick() -> None:
    cfg = load_config()
    block = cfg.get("SLOT_NEAR_MISSION_SPIN")
    if not isinstance(block, dict):
        return
    try:
        enabled = int(block.get("ENABLED", 0))
    except (TypeError, ValueError):
        enabled = 0
    if enabled != 1:
        return

    if not _cycle_lock.acquire(blocking=False):
        return
    try:
        _slot_near_mission_run_cycle(cfg, block)
    finally:
        _cycle_lock.release()


def _slot_near_mission_run_cycle(cfg: dict, block: dict) -> None:
    pick_n = max(1, _to_int(block.get("USER_PICK_COUNT", 3), 3))
    symbol_target = max(1, _to_int(block.get("SYMBOL_TARGET", 600), 600))
    min_bal = max(0, _to_int(block.get("BALANCE_MIN_BEFORE_SPIN", 100), 100))
    poll_sec = max(30, _to_int(block.get("DEPOSIT_POLL_MAX_SECONDS", 180), 180))
    skip_cd = max(15, _to_int(block.get("BALANCE_LOW_SKIP_COOLDOWN_SECONDS", 120), 120))
    skip_ws = _to_int(block.get("SKIP_IF_MINIGAME_WS", 1), 1) == 1
    sub_timeout = _to_int(block.get("SUBPROCESS_TIMEOUT_SECONDS", 0), 0)
    sub_kw: int | None = sub_timeout if sub_timeout > 0 else None

    raw_slots = block.get("SLOT_ORDER") or [39, 40]
    slots: List[int] = []
    for x in raw_slots if isinstance(raw_slots, list) else [39, 40]:
        try:
            slots.append(int(x))
        except (TypeError, ValueError):
            continue
    if not slots:
        slots = [39, 40]

    users = _fetch_playing_or_out_usernames()
    if not users:
        print("[SLOT_NV] (tick) Không có user Đang Chơi / Hết Tiền.", flush=True)
        return

    for u in users:
        _slot_nv_clear_balance_skip_if_recovered(u, min_bal)

    scored: List[Tuple[int, str]] = []
    for u in users:
        if _slot_nv_balance_skip_active(u):
            continue
        if skip_ws and _user_in_minigame_ws(u):
            continue
        if user_slot_client_lock_busy(u):
            continue
        s39 = _get_stats(u, 39)
        s40 = _get_stats(u, 40)
        if _user_skip_both_slots_done(s39, s40):
            continue
        stats_by_sid: Dict[int, dict | None] = {39: s39, 40: s40}
        for sid in slots:
            if sid not in stats_by_sid:
                stats_by_sid[sid] = _get_stats(u, sid)
        if not _user_needs_nv_any_slot(stats_by_sid, slots, symbol_target):
            continue
        score = _max_symbol_from_pair(s39, s40)
        scored.append((score, u))

    scored.sort(key=lambda t: (-t[0], t[1].lower()))
    picked: List[str] = []
    if min_bal <= 0:
        picked = [name for _, name in scored[:pick_n]]
    else:
        from get_balance import get_balance
        from auto_deposit_on_out_of_money import try_enqueue_deposit_if_cache_allows

        # Theo đúng thứ tự ưu tiên sau sort: user đứng đầu mà < min_bal vẫn xếp nạp rồi bỏ qua, lấy user kế.
        for _, u in scored:
            if len(picked) >= pick_n:
                break
            gb = get_balance(u, force=True)
            if gb.get("ok"):
                bal_u = _to_int(gb.get("balance"), 0)
                if bal_u < min_bal:
                    if try_enqueue_deposit_if_cache_allows(
                        u,
                        f"[SLOT_NV] số dư {bal_u:,}đ < {min_bal:,}đ — ưu tiên NV (định kỳ, bỏ qua tick)",
                    ):
                        print(
                            f"[SLOT_NV] 💳 [{u}] Đã xếp lệnh nạp (ưu tiên quay, số dư < {min_bal:,}đ).",
                            flush=True,
                        )
                    _slot_nv_set_balance_skip(u, skip_cd)
                    print(
                        f"[SLOT_NV] ⏸️ [{u}] Tạm loại khỏi ưu tiên NV {skip_cd}s "
                        f"(số dư {bal_u:,}đ < {min_bal:,}đ — chọn user khác nếu có).",
                        flush=True,
                    )
                    continue
            picked.append(u)

    if not picked:
        print(
            "[SLOT_NV] (tick) Không còn user phù hợp (đã NV / đủ symbol / số dư < ngưỡng / "
            "WS minigame / lock / lỗi stats).",
            flush=True,
        )
        return

    print(
        f"[SLOT_NV] ▶ Chọn {len(picked)}/{pick_n} user (ưu tiên symbol_count max 39/40, mốc {symbol_target}): "
        f"{', '.join(picked)}",
        flush=True,
    )

    for username in picked:
        if _slot_nv_shutdown_event.is_set():
            break
        if skip_ws and _user_in_minigame_ws(username):
            print(f"[SLOT_NV] ⏭️ [{username}] Đang WS minigame — bỏ qua.", flush=True)
            continue
        if user_slot_client_lock_busy(username):
            print(
                f"[SLOT_NV] ⏭️ [{username}] Đang có ws_slot_client khác (file lock) — bỏ qua.",
                flush=True,
            )
            continue
        s39 = _get_stats(username, 39)
        s40 = _get_stats(username, 40)
        if _user_skip_both_slots_done(s39, s40):
            print(f"[SLOT_NV] ⏭️ [{username}] Đã reward_claimed cả 39 và 40 hôm nay.", flush=True)
            continue

        if min_bal > 0:
            if not _ensure_balance_min(username, min_bal, poll_sec, skip_cd):
                print(f"[SLOT_NV] ⏭️ [{username}] Bỏ qua quay (số dư).", flush=True)
                continue

        for sid in slots:
            if _slot_nv_shutdown_event.is_set():
                break
            if skip_ws and _user_in_minigame_ws(username):
                print(f"[SLOT_NV] ⏭️ [{username}] WS minigame xen giữa — dừng slot.", flush=True)
                break
            sym, rc, same = _stats_slice(_get_stats(username, sid))
            if same and rc == 1:
                print(f"[SLOT_NV] ⏭️ [{username}] slot {sid} đã reward_claimed — skip.", flush=True)
                continue
            if _slot_nv_shutdown_event.is_set():
                break
            print(f"[SLOT_NV] 🎰 [{username}] --slot {sid} (symbol~{sym})", flush=True)
            _run_ws_slot_client(username, sid, sub_kw)
        if _slot_nv_shutdown_event.is_set():
            break


def slot_near_mission_scheduler_loop() -> None:
    print(
        "[SLOT_NV] Scheduler khởi động (SLOT_NEAR_MISSION_SPIN.ENABLED=1, interval từ config).",
        flush=True,
    )
    while not _slot_nv_shutdown_event.is_set():
        sleep_s = 60
        try:
            cfg = load_config()
            b = cfg.get("SLOT_NEAR_MISSION_SPIN")
            if isinstance(b, dict):
                sleep_s = max(15, _to_int(b.get("INTERVAL_SECONDS", 60), 60))
        except Exception:
            sleep_s = 60

        try:
            slot_near_mission_tick()
        except Exception as e:
            print(f"[SLOT_NV] ❌ tick: {e}", flush=True)
            import traceback

            traceback.print_exc()
        if _slot_nv_shutdown_event.wait(timeout=sleep_s):
            break


def start_slot_near_mission_scheduler() -> None:
    threading.Thread(target=slot_near_mission_scheduler_loop, daemon=True).start()
