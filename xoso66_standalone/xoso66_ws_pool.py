# -*- coding: utf-8 -*-
"""
Chọn account mở WS mini-game + nạp khi thiếu acc đủ điều kiện.

Bù slot WS / mở WS / nạp (sort_rows_ws_fill_priority):
  game_worker.ws_fill_priority = 1:
    - Đoạn 1: balance >= bet_step — balance cao → thấp.
    - Đoạn 2: balance < bet_step — tổng cược ngày thấp → cao.
  ws_fill_priority = 0: mọi acc chung một list — balance thấp → cao, bằng nhau cược ngày cao → thấp.
  ws_fill_priority = 2: mọi acc chung một list — cược ngày cao → thấp, bằng nhau số dư cao → thấp
    (thiếu tiền vẫn theo thứ tự này → nạp rồi mở WS).

Gán cược mỗi phiên vẫn theo assign_strategy (xoso66_bet_assign).

Đầu phiên / resync — 3 cổng đơn (tránh tranh việc):
  - apply_account_status_intent: mọi ép Hết Tiền / Đủ ngày / Đang Chơi
  - request_ws_close: mọi đóng WS + unregister
  - request_ws_open: mọi xếp mở WS (sau khi đã Đang Chơi)
  Orchestrator reconcile_ws_pool: Phiên mới = 5 việc (song song):
    1) DB < min, ∉ C → Hết Tiền
    2) đủ cap cược ngày → Đủ ngày
    3) bù Đang Chơi (trừ D list nạp)
    4) Đang Chơi ∉ A → Open WS
    5) A ∉ Đang Chơi (∉ C) → Close WS
  Trần ws_account_count đếm «Đang Chơi» — WS theo sát status.

Quy chuẩn A / B / C / D:
  A = nick đang giữ WS (connected ∪ pending mở)
  B = cổng mở WS (request_ws_open)
  C = chờ KQ (pending_bet) — HOLD: không ép status / không đóng WS
  D = list nạp (cache / đã xếp nạp) — chiếm chỗ bù Đang Chơi
  Thiếu tiền / đủ ngày / bù / open / close: chỉ log khi có thay đổi.
  Thắng DB < min: win-credit rồi mới Hết Tiền (khác luồng thua).

Ngưỡng tiền WS = bet_step_vnd; cap cược ngày WS = daily_bet_cap_vnd (897k…).
side_total_vnd chỉ dùng chia cược mỗi phiên Tài/Xỉu, không dùng «Đủ ngày».
List ưu tiên (API / nạp): chỉ Hết Tiền / Đủ ngày (+ proxy). Pool chính: «Đang Chơi».
Không WS: status «Lỗi» (WS_BLOCKED_STATUSES).
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from xoso66_accounts_db import (
    STATUS_DANG_CHOI,
    STATUS_DU_NGAY,
    STATUS_HET_TIEN,
    STATUS_KHOA,
    STATUS_LOI,
    STATUS_LOI_PROXY,
    daily_bet_today_vnd as _daily_bet_from_row,
    get_account,
    list_accounts,
    list_accounts_by_status,
    set_account_status,
    username_for_log,
    usernames_for_log,
)

# Không mở WS / không WS listener — kể cả có proxy.
WS_BLOCKED_STATUSES = frozenset({STATUS_LOI, STATUS_LOI_PROXY, STATUS_KHOA})

_ws_task_ids_provider: Callable[[], list[str]] | None = None
_connected_ws_ids: set[str] = set()
_connected_ws_gens: dict[str, str] = {}
_connected_ws_lock = threading.Lock()
_pool_cache: list[str] = []
_pool_cache_lock = threading.Lock()
_selection_logged = False
_pool_startup_done = False
_pool_startup_lock = threading.Lock()
_ws_deposit_scheduled: set[str] = set()
_ws_deposit_scheduled_lock = threading.Lock()
_ws_round_sync_enabled = False
_ws_round_sync_lock = threading.Lock()
_pending_slot_ids: set[str] = set()
_pending_slot_lock = threading.Lock()
_ws_listener_id: str | None = None
_ws_listener_lock = threading.Lock()
_round_balance_recheck_pending: dict[str, float] = {}
_round_balance_recheck_lock = threading.Lock()
# Nick thắng phiên, DB < min — đang chờ thưởng về (multi-retry).
_win_credit_recheck_since: dict[str, float] = {}
_win_credit_recheck_lock = threading.Lock()
_ws_slot_log_last: dict[str, tuple] = {}
_ws_slot_log_lock = threading.Lock()
_assign_strategy_switch_at: float = 0.0
_assign_strategy_switch_lock = threading.Lock()
# Conflict 1 nick / 1 vòng reconcile: "close" | "open" | "hold"
_ws_reconcile_action: dict[str, str] = {}
_ws_reconcile_action_lock = threading.Lock()


def _reconcile_begin() -> None:
    with _ws_reconcile_action_lock:
        _ws_reconcile_action.clear()


def _reconcile_set_action(aid: str, action: str) -> bool:
    """True nếu nhận action; False nếu đã HOLD hoặc open rồi lại close."""
    aid = str(aid).strip()
    if not aid:
        return False
    action = str(action or "").strip().lower()
    with _ws_reconcile_action_lock:
        prev = _ws_reconcile_action.get(aid)
        if prev == "hold":
            return False
        if action == "hold":
            _ws_reconcile_action[aid] = "hold"
            return True
        # close rồi open lại (zombie → must_open) được phép.
        if prev == "close" and action == "open":
            _ws_reconcile_action[aid] = "reopen"
            return True
        if prev in ("open", "reopen") and action == "close":
            return False
        _ws_reconcile_action[aid] = action
        return True


def apply_account_status_intent(
    account_id: str,
    status: str,
    *,
    reason: str = "",
) -> bool:
    """
    Cổng duy nhất đổi status pool (Hết Tiền / Đủ ngày / Đang Chơi).
    Nick ∈ C (chờ KQ) → không đổi status.
    """
    aid = str(account_id).strip()
    new_st = str(status or "").strip()
    if not aid or not new_st:
        return False
    if aid in list_C_pending_kq_ids():
        return False
    tag = f" — {reason}" if reason else ""
    _ = tag
    return set_account_status(aid, new_st, reason=reason)


def request_ws_close(
    account_ids: list[str],
    *,
    reason: str = "",
    flow: str = "",
    cfg: dict[str, Any] | None = None,
    quiet: bool = True,
) -> list[str]:
    """
    Cổng duy nhất đóng WS: clear pending + unregister ngay.
    Im lặng mặc định (quiet=True) — tránh spam chồng với việc5 / leave pool.
    """
    if cfg is None:
        from xoso66_config_util import load_config

        cfg = load_config()
    out: list[str] = []
    bet = _pending_bet_skip_ids()
    for aid in account_ids:
        aid = str(aid).strip()
        if not aid or aid in bet:
            continue
        if is_ws_listener(aid, cfg):
            continue
        if not _reconcile_set_action(aid, "close"):
            continue
        clear_pending_ws_slot(aid)
        unregister_ws_connected(aid)
        out.append(aid)
    _ = reason, flow, quiet
    return out


def request_ws_open(
    account_ids: list[str],
    *,
    reason: str = "",
    cfg: dict[str, Any] | None = None,
    ensure_dang_choi: bool = True,
    quiet: bool = False,
) -> list[str]:
    """
    Cổng duy nhất xếp mở WS: ensure Đang Chơi + mark pending.
    Trả list aid được OPEN (worker spawn qua fill_connect / apply_pool).
    """
    if cfg is None:
        from xoso66_config_util import load_config

        cfg = load_config()
    out: list[str] = []
    for aid in account_ids:
        aid = str(aid).strip()
        if not aid:
            continue
        if is_ws_listener(aid, cfg):
            continue
        if not _reconcile_set_action(aid, "open"):
            continue
        if ensure_dang_choi:
            row = get_account(aid) or {}
            st = str(row.get("status") or "").strip()
            if st != STATUS_DANG_CHOI and not is_ws_blocked_status(st):
                apply_account_status_intent(
                    aid, STATUS_DANG_CHOI, reason=reason or "mở WS"
                )
        mark_pending_ws_slots([aid])
        out.append(aid)
    if out and not quiet:
        names = ", ".join(username_for_log(a) for a in out[:12])
        extra = f"… +{len(out) - 12}" if len(out) > 12 else ""
        why = f" — {reason}" if reason else ""
        print(f"[WS-OPEN] {names}{extra}{why}", flush=True)
    return out


def _assign_strategy_switch_cooldown_sec(cfg: dict[str, Any]) -> float:
    ab = cfg.get("auto_bet")
    if isinstance(ab, dict) and ab.get("assign_strategy_switch_cooldown_sec") is not None:
        return max(60.0, float(ab.get("assign_strategy_switch_cooldown_sec") or 600))
    return max(
        60.0,
        float(os.environ.get("XOSO66_ASSIGN_STRATEGY_SWITCH_COOLDOWN", "600")),
    )


def _assign_strategy_switch_allowed(cfg: dict[str, Any]) -> bool:
    with _assign_strategy_switch_lock:
        elapsed = time.time() - _assign_strategy_switch_at
    return elapsed >= _assign_strategy_switch_cooldown_sec(cfg)


def _mark_assign_strategy_switched() -> None:
    global _assign_strategy_switch_at
    with _assign_strategy_switch_lock:
        _assign_strategy_switch_at = time.time()


def mark_pending_ws_slots(account_ids: list[str]) -> None:
    """Đánh dấu slot đang mở WS / nạp (trước khi connect hoặc HTTP nạp xong)."""
    with _pending_slot_lock:
        for aid in account_ids:
            s = str(aid).strip()
            if s:
                _pending_slot_ids.add(s)


def clear_pending_ws_slot(account_id: str) -> None:
    with _pending_slot_lock:
        _pending_slot_ids.discard(str(account_id).strip())


def discard_ws_deposit_scheduled(account_id: str) -> None:
    """Bỏ nick khỏi hàng nạp WS-pool (Huỷ / thất bại bên thứ 3)."""
    aid = str(account_id).strip()
    if not aid:
        return
    with _ws_deposit_scheduled_lock:
        _ws_deposit_scheduled.discard(aid)


def release_ws_blocks_after_deposit(account_ids: list[str]) -> None:
    """
    Bỏ pending slot / cache / reserve / hàng nạp WS — Huỷ, thất bại, hoặc sau Hoàn tất.
    """
    from xoso66_accounts_db import username_for_log

    for aid in account_ids:
        aid = str(aid).strip()
        if not aid:
            continue
        discard_ws_deposit_scheduled(aid)
        clear_pending_ws_slot(aid)
        cache_cleared = False
        try:
            from xoso66_auto_deposit import release_deposit_reserve, remove_from_deposit_cache

            release_deposit_reserve(aid, clear_cache=True)
            cache_cleared = remove_from_deposit_cache(aid)
        except Exception:
            try:
                from xoso66_auto_deposit import remove_from_deposit_cache

                cache_cleared = remove_from_deposit_cache(aid)
            except Exception:
                pass
        if cache_cleared:
            print(
                f"[WS-POOL] Đã xóa cache nạp {username_for_log(aid)}",
                flush=True,
            )


def get_pending_ws_slot_ids() -> set[str]:
    with _pending_slot_lock:
        return set(_pending_slot_ids)


def register_ws_task_ids_provider(provider: Callable[[], list[str]] | None) -> None:
    """Đăng ký nguồn task WS thật (supervisor.tasks.keys) — thay roster mục tiêu cũ."""
    global _ws_task_ids_provider
    _ws_task_ids_provider = provider


def get_ws_task_accounts() -> list[str]:
    """Nick đang có asyncio task WS (nếu worker đã đăng ký provider)."""
    if _ws_task_ids_provider is None:
        return []
    try:
        return [
            str(x).strip()
            for x in _ws_task_ids_provider()
            if str(x).strip()
        ]
    except Exception:
        return []


def get_ws_pool_scope_ids() -> list[str]:
    """Pool thực tế: task ∪ connect ∪ pending (không roster RAM)."""
    scope: set[str] = set(get_ws_task_accounts())
    scope |= {str(x).strip() for x in get_connected_ws_accounts() if str(x).strip()}
    scope |= get_pending_ws_slot_ids()
    return sorted(scope)


def ws_slots_exclude_ids(
    cfg: dict[str, Any], *, extra: set[str] | None = None
) -> set[str]:
    """Nick đã chiếm slot — không chọn lại khi bù."""
    out = ws_target_occupied_account_ids(cfg) | get_pending_ws_slot_ids()
    lid = pick_ws_listener_account(cfg)
    if lid:
        out.add(lid)
    if extra:
        out |= {str(x).strip() for x in extra if str(x).strip()}
    return out


def dang_choi_account_ids(
    cfg: dict[str, Any],
    *,
    exclude: set[str] | list[str] | None = None,
) -> set[str]:
    """
    Mọi nick status «Đang Chơi» (trừ listener / exclude).
    Đây là nguồn đếm trần pool — WS phải theo sát tập này.
    """
    ex = {str(x).strip() for x in (exclude or []) if str(x).strip()}
    lid = pick_ws_listener_account(cfg)
    if lid:
        ex.add(lid)
    status = ws_account_status(cfg)
    out: set[str] = set()
    for row in list_accounts_by_status(status):
        aid = str(row.get("id") or "").strip()
        if not aid or aid in ex:
            continue
        if is_ws_blocked_status(str(row.get("status") or "")):
            continue
        out.add(aid)
    return out


def count_ws_slots_in_use(
    cfg: dict[str, Any],
    *,
    task_ids: list[str] | None = None,
    exclude: set[str] | list[str] | None = None,
) -> dict[str, Any]:
    """
    Đếm trần pool = số nick «Đang Chơi».
    connect/task/pending chỉ để debug — không dùng làm in_use.
    """
    ex = {str(x).strip() for x in (exclude or []) if str(x).strip()}
    dang = dang_choi_account_ids(cfg, exclude=ex)
    tasks = {
        str(x).strip()
        for x in (task_ids if task_ids is not None else get_ws_task_accounts())
        if str(x).strip() and str(x).strip() not in ex
    }
    connected = {
        str(x).strip()
        for x in get_connected_ws_accounts()
        if str(x).strip() and str(x).strip() not in ex
    }
    pending = {a for a in get_pending_ws_slot_ids() if a not in ex}
    caching = {a for a in deposit_cache_account_ids(cfg) if a not in ex}
    with _ws_deposit_scheduled_lock:
        scheduling = {
            str(x).strip()
            for x in _ws_deposit_scheduled
            if str(x).strip() and str(x).strip() not in ex
        }
    zombie = tasks - connected - pending
    missing_ws = dang - connected
    return {
        "dang_choi_n": len(dang),
        "dang_choi_ids": sorted(dang),
        "task_n": len(tasks),
        "connected_n": len(connected),
        "deposit_cache_n": len(caching),
        "deposit_cache_ids": sorted(caching),
        "deposit_cache_extra_n": len(caching - dang),
        "pending_n": len(pending),
        "pending_ids": sorted(pending),
        "scheduling_n": len(scheduling),
        "scheduling_ids": sorted(scheduling),
        "ws_live_n": len(connected),
        "ready_n": len(connected),
        "zombie_task_n": len(zombie),
        "missing_ws_n": len(missing_ws),
        "in_use_n": len(dang),
    }


def ws_slots_need_fill(
    cfg: dict[str, Any],
    *,
    task_ids: list[str] | None = None,
    exclude: set[str] | list[str] | None = None,
) -> int:
    """Thiếu bao nhiêu chỗ tới cap — Đang Chơi ∪ D (đang nạp)."""
    _ = task_ids
    target = ws_account_count(cfg)
    occupied = ws_target_occupied_account_ids(cfg)
    ex = {str(x).strip() for x in (exclude or []) if str(x).strip()}
    if ex:
        occupied = {a for a in occupied if a not in ex}
    return max(0, target - len(occupied))


def ws_pool_connected_count(cfg: dict[str, Any]) -> int:
    """Số nick pool WS đã connect (bỏ listener — không tính vào ws_account_count)."""
    connected = {
        str(x).strip() for x in get_connected_ws_accounts() if str(x).strip()
    }
    lid = pick_ws_listener_account(cfg)
    if lid:
        connected.discard(lid)
    return len(connected)


def ws_pool_focus_game_id(cfg: dict[str, Any] | None = None) -> int | None:
    """game_id đang theo dõi = hũ cao nhất (focus) — định nghĩa «Phiên mới» cho WS pool."""
    from xoso66_jackpot_picker import focus_game_id

    return focus_game_id(cfg)


def round_start_triggers_ws_pool_resync(game_id: int, cfg: dict[str, Any] | None = None) -> bool:
    """
    Phiên mới = BẮT ĐẦU phiên của game đang theo dõi (hũ cao nhất / focus).

    Không phụ thuộc auto-bet có cược hay hủy phiên — chỉ cần đúng game focus.
    """
    if cfg is None:
        from xoso66_config_util import load_config

        cfg = load_config()
    gid = int(game_id)
    focus = ws_pool_focus_game_id(cfg)
    if focus is not None:
        return gid == int(focus)
    try:
        from xoso66_jackpot_picker import highest_jackpot_game

        top = highest_jackpot_game(cfg)
        if top is not None:
            return gid == int(top.game_id)
    except Exception:
        pass
    try:
        from xoso66_playing_game_store import load_playing_game

        pg = load_playing_game()
        if pg and pg.get("game_id") is not None:
            return gid == int(pg["game_id"])
    except Exception:
        pass
    return False


def _format_ws_slot_users_label(
    account_ids: set[str] | list[str], *, max_show: int = 8
) -> str:
    """Nhãn log cache/pending/sched — ví dụ (user1,user2)."""
    ids = sorted(str(x).strip() for x in account_ids if str(x).strip())
    if not ids:
        return ""
    names = usernames_for_log(ids[:max_show])
    if len(ids) > max_show:
        names.append(f"+{len(ids) - max_show}")
    return f"({','.join(names)})"


def _log_ws_slot_need(
    cfg: dict[str, Any],
    *,
    task_ids: list[str] | None,
    need: int,
    log_context: str,
) -> None:
    if not log_context:
        return
    if need <= 0:
        return
    info = count_ws_slots_in_use(cfg, task_ids=task_ids)
    target = ws_account_count(cfg)
    live = ws_pool_connected_count(cfg)
    snapshot = (
        int(info.get("dang_choi_n", info["in_use_n"])),
        int(info["connected_n"]),
        int(info.get("missing_ws_n", 0)),
        int(info["deposit_cache_n"]),
        int(info["pending_n"]),
        int(info.get("scheduling_n", 0)),
        int(live),
        int(info["in_use_n"]),
        int(need),
        int(target),
    )
    with _ws_slot_log_lock:
        if _ws_slot_log_last.get(log_context) == snapshot:
            return
        _ws_slot_log_last[log_context] = snapshot
    cache_lbl = _format_ws_slot_users_label(info.get("deposit_cache_ids") or [])
    pending_lbl = _format_ws_slot_users_label(info.get("pending_ids") or [])
    sched_lbl = _format_ws_slot_users_label(info.get("scheduling_ids") or [])
    print(
        f"[WS-POOL] {log_context}: đang_chơi={info.get('dang_choi_n', info['in_use_n'])}/{target} "
        f"connect={info['connected_n']} thiếu_ws={info.get('missing_ws_n', 0)} "
        f"task={info['task_n']} zombie={info.get('zombie_task_n', 0)} "
        f"cache={info['deposit_cache_n']}{cache_lbl} "
        f"pending={info['pending_n']}{pending_lbl} "
        f"sched={info.get('scheduling_n', 0)}{sched_lbl} → bù {need}",
        flush=True,
    )


def game_worker_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    raw = cfg.get("game_worker")
    return raw if isinstance(raw, dict) else {}


def ws_account_count(cfg: dict[str, Any]) -> int:
    """Mục tiêu tối thiểu nick WS đã connect (chỉ connect mới được chơi)."""
    gw = game_worker_cfg(cfg)
    return max(1, int(gw.get("ws_account_count") or 12))


def ws_listener_enabled(cfg: dict[str, Any]) -> bool:
    """Luôn giữ 1 WS nghe phiên/hũ (qua đêm, không evict cap)."""
    gw = game_worker_cfg(cfg)
    return bool(gw.get("ws_listener_enabled", True))


def pick_ws_listener_account(cfg: dict[str, Any]) -> str | None:
    """Chọn 1 acc có proxy để giữ WS — không cần đủ tiền / cap ngày."""
    global _ws_listener_id
    if not ws_listener_enabled(cfg):
        return None
    gw = game_worker_cfg(cfg)
    from xoso66_config_util import resolve_ws_listener_account_id, ws_listener_username

    forced = resolve_ws_listener_account_id(cfg)
    explicit_preferred = bool(
        str(gw.get("ws_listener_account_id") or "").strip()
        or str(gw.get("ws_listener_username") or "").strip()
        or ws_listener_username(cfg)
    )
    with _ws_listener_lock:
        if forced:
            row = get_account(forced) or {}
            if row_allowed_for_ws(row):
                _ws_listener_id = forced
                return forced
            if explicit_preferred:
                st = str(row.get("status") or "").strip()
                if is_ws_blocked_status(st):
                    print(
                        f"[WS-LISTENER] Ưu tiên {username_for_log(forced)} — "
                        f"status={st!r} (không mở WS listener)",
                        flush=True,
                    )
                else:
                    print(
                        f"[WS-LISTENER] Ưu tiên {username_for_log(forced)} — "
                        "thiếu proxy trong DB (không fallback nick khác)",
                        flush=True,
                    )
                _ws_listener_id = None
                return None
        if _ws_listener_id:
            row = get_account(_ws_listener_id) or {}
            if row_allowed_for_ws(row):
                return _ws_listener_id
            _ws_listener_id = None
        for row in list_accounts():
            aid = str(row.get("id") or "").strip()
            if not aid or not row_allowed_for_ws(row):
                continue
            _ws_listener_id = aid
            return aid
        _ws_listener_id = None
    return None


def is_ws_listener(account_id: str, cfg: dict[str, Any] | None = None) -> bool:
    aid = str(account_id or "").strip()
    if not aid:
        return False
    with _ws_listener_lock:
        if _ws_listener_id and aid == _ws_listener_id:
            return True
    if cfg is not None and ws_listener_enabled(cfg):
        from xoso66_config_util import resolve_ws_listener_account_id

        forced = resolve_ws_listener_account_id(cfg)
        if forced and aid == forced:
            return True
    return False


def filter_ws_evict_ids(account_ids: list[str], cfg: dict[str, Any]) -> list[str]:
    """Bỏ WS listener — không ngắt khi đủ cap / thiếu tiền."""
    return [a for a in account_ids if not is_ws_listener(a, cfg)]


def ws_pool_resync_enabled(cfg: dict[str, Any]) -> bool:
    gw = game_worker_cfg(cfg)
    return bool(gw.get("ws_pool_resync_enabled", True))


def ws_pool_resync_interval_sec(cfg: dict[str, Any]) -> int:
    gw = game_worker_cfg(cfg)
    return max(10, int(gw.get("ws_pool_resync_interval_sec") or 60))


def ws_connect_batch_size(cfg: dict[str, Any]) -> int:
    """Số nick WS connect đồng thời. Windows: luôn 1 (LC79 socks gate)."""
    import sys

    if sys.platform.startswith("win"):
        return 1
    gw = game_worker_cfg(cfg)
    return max(1, int(gw.get("ws_connect_batch_size") or 4))


def ws_connect_batch_delay_sec(cfg: dict[str, Any]) -> float:
    """Delay giữa các spawn WS. Windows mặc định 0.5s (LC79 stagger)."""
    import sys

    gw = game_worker_cfg(cfg)
    if sys.platform.startswith("win"):
        return max(0.5, float(gw.get("ws_connect_batch_delay_sec") or 0.5))
    return max(0.0, float(gw.get("ws_connect_batch_delay_sec") or 0.2))


def ws_bulk_refresh_threshold(cfg: dict[str, Any]) -> int:
    """Trên ngưỡng này: không refresh token từng nick (startup đã ping)."""
    gw = game_worker_cfg(cfg)
    return max(1, int(gw.get("ws_bulk_refresh_threshold") or 5))


def ws_pool_resync_only_expand(cfg: dict[str, Any]) -> bool:
    """
    (Dự phòng) Resync định kỳ: chỉ mở thêm nick, không tự đóng WS thừa.
    """
    gw = game_worker_cfg(cfg)
    return bool(gw.get("ws_pool_resync_only_expand", True))


def is_row_eligible_for_ws(
    row: dict[str, Any], *, min_bal: int, daily_limit: float
) -> bool:
    return (
        daily_bet_today_vnd(row) < daily_limit
        and account_balance_vnd(row) >= min_bal
    )


def is_row_deposit_candidate(
    row: dict[str, Any], *, min_bal: int, daily_limit: float
) -> bool:
    return (
        daily_bet_today_vnd(row) < daily_limit
        and account_balance_vnd(row) < min_bal
    )


def is_row_exhausted_daily_cap(
    row: dict[str, Any], *, daily_limit: float
) -> bool:
    """Không còn đặt được mức bet_step nhỏ nhất (cược ngày >= cap - step)."""
    return daily_bet_today_vnd(row) >= daily_limit


def status_when_leaving_ws(row: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    """Trạng thái ép khi ngắt WS; None = không đổi."""
    daily_limit = float(daily_bet_ws_limit_vnd(cfg))
    min_bal = min_balance_for_ws(cfg)
    if daily_bet_today_vnd(row) >= daily_limit:
        return STATUS_DU_NGAY
    if account_balance_vnd(row) < min_bal:
        return STATUS_HET_TIEN
    return None


def sync_status_for_ws_pool_change(
    cfg: dict[str, Any],
    *,
    joining: list[str] | None = None,
    leaving: list[str] | None = None,
) -> None:
    """
    Leave pool: chỉ đóng WS — không ép Hết Tiền/Đủ ngày (để Phiên mới / mark_daily_cap).
    Join: set Đang Chơi nếu chưa.
    """
    leave_ids = filter_ws_evict_ids(leaving or [], cfg)
    if leave_ids:
        request_ws_close(
            leave_ids,
            reason="leave pool — chỉ đóng WS (không đổi status)",
            flow="sync_status_for_ws_pool_change.leaving",
            cfg=cfg,
        )

    for aid in joining or []:
        aid = str(aid).strip()
        if not aid:
            continue
        row = get_account(aid) or {}
        st = str(row.get("status") or "").strip()
        if st == STATUS_DANG_CHOI:
            continue
        if is_ws_blocked_status(st):
            continue
        apply_account_status_intent(aid, STATUS_DANG_CHOI, reason="mở WS")


def mark_daily_cap_status(account_ids: list[str], cfg: dict[str, Any]) -> None:
    """Ép Đủ ngày qua cổng Status + Close."""
    aids = [str(x).strip() for x in account_ids if str(x).strip()]
    if not aids:
        return
    for aid in aids:
        apply_account_status_intent(aid, STATUS_DU_NGAY, reason="đủ cap cược ngày")
    request_ws_close(
        aids,
        reason="đủ cap cược ngày → Đủ ngày",
        flow="mark_daily_cap_status",
        cfg=cfg,
    )


def reschedule_mission_claims_for_cap(cfg: dict[str, Any]) -> list[str]:
    """
    No-op (giữ API cũ).

    Không hẹn claim khi nâng daily_bet_cap — tránh spam acc đã xong mốc 890k
    trong khi chưa tới mốc mới (mini game ~2690k). Claim chỉ khi
    set_account_status → «Đủ ngày» (đủ cap / ngắt WS).
    """
    _ = cfg
    return []


def sync_du_ngay_under_cap_to_dang_choi(cfg: dict[str, Any]) -> list[str]:
    """Alias cũ — không promote Đang Chơi; không reclaim mission."""
    return reschedule_mission_claims_for_cap(cfg)


def sync_exhausted_dang_choi_to_du_ngay(cfg: dict[str, Any]) -> list[str]:
    """
    «Đang Chơi» đã hết room cap (daily >= cap - bet_step) → Đủ ngày + hẹn nhận thưởng.
    Chạy kể cả nick chưa mở WS (trước đây chỉ đổi status khi ngắt WS / gán cược).
    Claim gắn ở set_account_status(Đủ ngày) — không reclaim hàng loạt khi nâng cap.
    """
    daily_limit = float(daily_bet_ws_limit_vnd(cfg))
    status = ws_account_status(cfg)
    exhausted: list[str] = []
    for row in list_accounts_by_status(status):
        aid = str(row.get("id") or "").strip()
        if not aid:
            continue
        if is_row_exhausted_daily_cap(row, daily_limit=daily_limit):
            exhausted.append(aid)
    if not exhausted:
        return []
    mark_daily_cap_status(exhausted, cfg)
    ws_live: set[str] = set()
    for aid in get_ws_task_accounts():
        s = str(aid).strip()
        if s:
            ws_live.add(s)
    for aid in get_connected_ws_accounts():
        s = str(aid).strip()
        if s:
            ws_live.add(s)
    to_evict = [
        a for a in exhausted if a in ws_live and not is_ws_listener(a, cfg)
    ]
    if to_evict:
        request_ws_evict_and_resync(to_evict)
    return exhausted


# Chỉ các status này được đưa vào list ưu tiên mở WS / nạp (bước 4, API).
WS_OPEN_LIST_STATUSES = frozenset({STATUS_HET_TIEN, STATUS_DU_NGAY})


def is_ws_blocked_status(status: str) -> bool:
    """True nếu acc không được dùng cho WS (vd. «Lỗi»)."""
    return str(status or "").strip() in WS_BLOCKED_STATUSES


def row_has_ws_proxy(row: dict[str, Any]) -> bool:
    return bool(str(row.get("proxy") or "").strip())


def row_allowed_for_ws(row: dict[str, Any]) -> bool:
    """Có proxy và không bị chặn WS (Lỗi, …)."""
    if is_ws_blocked_status(str(row.get("status") or "")):
        return False
    return row_has_ws_proxy(row)


def _pool_rows(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Pool «Đang Chơi» + proxy — bước 3 đầu phiên (mở WS acc đang chơi)."""
    status = ws_account_status(cfg)
    pool = list_accounts_by_status(status)
    return [a for a in pool if row_allowed_for_ws(a)]


def _pool_rows_ws_open_list(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """
    List ưu tiên mở WS: chỉ Hết Tiền / Đủ ngày + proxy.
    Loại nick status khác (khoá, Đang Chơi, …).
    """
    _ = cfg
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for st in sorted(WS_OPEN_LIST_STATUSES):
        for row in list_accounts_by_status(st):
            aid = str(row.get("id") or "").strip()
            if not aid or aid in seen:
                continue
            if str(row.get("status") or "").strip() not in WS_OPEN_LIST_STATUSES:
                continue
            if not row_allowed_for_ws(row):
                continue
            seen.add(aid)
            out.append(row)
    return out


def row_allowed_for_ws_open_list(row: dict[str, Any]) -> bool:
    st = str(row.get("status") or "").strip()
    return st in WS_OPEN_LIST_STATUSES and row_allowed_for_ws(row)


def ws_funded_account_ids(cfg: dict[str, Any], current_ids: list[str]) -> list[str]:
    """Acc đang WS: >= min_balance và cược ngày < cap - bet_step."""
    min_bal = min_balance_for_ws(cfg)
    daily_limit = float(daily_bet_ws_limit_vnd(cfg))
    out: list[str] = []
    for aid in current_ids:
        aid = str(aid).strip()
        if not aid:
            continue
        row = get_account(aid) or {}
        if is_row_eligible_for_ws(row, min_bal=min_bal, daily_limit=daily_limit):
            out.append(aid)
    return out


def _deposit_cache_ttl_sec(cfg: dict[str, Any]) -> int:
    ad = cfg.get("auto_deposit")
    if isinstance(ad, dict) and ad.get("cache_ttl_sec") is not None:
        return int(ad.get("cache_ttl_sec") or 900)
    return 900


def deposit_cache_account_ids(cfg: dict[str, Any]) -> set[str]:
    """Acc đang trong deposit_pending_cache (còn TTL) — giống LC79 +cache nạp."""
    ttl = _deposit_cache_ttl_sec(cfg)
    now = time.time()
    out: set[str] = set()
    try:
        from xoso66_auto_deposit import load_deposit_cache

        for aid, ts in load_deposit_cache().items():
            aid = str(aid).strip()
            if aid and (now - float(ts)) < ttl:
                out.add(aid)
    except Exception:
        pass
    return out


def list_D_deposit_ids(cfg: dict[str, Any]) -> set[str]:
    """
    List D — đang nạp / đã xếp nạp (cache ∪ scheduled).
    Chiếm chỗ bù «Đang Chơi» ở BƯỚC1 (nạp xong → Đang Chơi).
    """
    out = set(deposit_cache_account_ids(cfg))
    with _ws_deposit_scheduled_lock:
        out |= {str(x).strip() for x in _ws_deposit_scheduled if str(x).strip()}
    lid = pick_ws_listener_account(cfg)
    if lid:
        out.discard(lid)
    return out


def ws_target_occupied_account_ids(cfg: dict[str, Any]) -> set[str]:
    """Đã chiếm chỗ bù: «Đang Chơi» ∪ D (đang nạp) — tránh chọn trùng / vượt cap."""
    return dang_choi_account_ids(cfg) | list_D_deposit_ids(cfg)


def ws_target_occupied_counts(cfg: dict[str, Any]) -> dict[str, int]:
    info = count_ws_slots_in_use(cfg)
    return {
        "task_n": int(info["task_n"]),
        "connected_n": int(info["connected_n"]),
        "deposit_cache_n": int(info["deposit_cache_n"]),
        "deposit_cache_extra_n": int(info.get("deposit_cache_extra_n", 0)),
        "pending_n": int(info["pending_n"]),
        "dang_choi_n": int(info.get("dang_choi_n", info["in_use_n"])),
        "occupied_n": int(info["in_use_n"]),
    }


def is_ws_deposit_thread_scheduled(account_id: str) -> bool:
    """Luồng nạp WS-pool đã lên lịch (chưa tới cache/DB)."""
    aid = str(account_id).strip()
    if not aid:
        return False
    with _ws_deposit_scheduled_lock:
        return aid in _ws_deposit_scheduled


def account_deposit_in_flight(account_id: str, cfg: dict[str, Any]) -> bool:
    """
    Đang nạp (LC79: deposit_pending_cache + lệnh DB chưa terminal).
    Dùng khi đếm slot ws_account_count — tránh nạp thêm khi đã đủ 12 «chỗ».
    """
    aid = str(account_id).strip()
    if not aid:
        return False
    try:
        from xoso66_auto_deposit import is_deposit_reserved

        if is_deposit_reserved(aid):
            return True
    except Exception:
        pass
    ttl = _deposit_cache_ttl_sec(cfg)
    try:
        from xoso66_auto_deposit import load_deposit_cache

        cache = load_deposit_cache()
        ts = cache.get(aid)
        if ts and (time.time() - float(ts)) < ttl:
            return True
    except Exception:
        pass
    try:
        from xoso66_deposit_orders_db import has_pending_deposit

        if has_pending_deposit(aid, max_age_sec=ttl):
            return True
    except Exception:
        pass
    return False


def account_ws_deposit_busy(account_id: str, cfg: dict[str, Any]) -> bool:
    """Đang nạp hoặc luồng nạp WS-pool đã lên lịch — dùng khi chọn nick / giữ pending."""
    return is_ws_deposit_thread_scheduled(account_id) or account_deposit_in_flight(
        account_id, cfg
    )


def ws_live_in_scope(scope_ids: list[str] | set[str]) -> set[str]:
    """Nick WS active hoặc đã connect (trong danh sách scope)."""
    scope = {str(x).strip() for x in scope_ids if str(x).strip()}
    if not scope:
        return set()
    live: set[str] = set()
    for aid in get_ws_task_accounts():
        if aid in scope:
            live.add(aid)
    for aid in get_connected_ws_accounts():
        if aid in scope:
            live.add(aid)
    return live


def ws_pool_slot_breakdown(
    cfg: dict[str, Any], scope_ids: list[str]
) -> dict[str, Any]:
    """
    Đếm slot pool (giống LC79: WS + đã đủ tiền + đang nạp).
    Trả funded / depositing / ws_only / occupied (hợp không trùng).
    """
    scope = [str(x).strip() for x in scope_ids if str(x).strip()]
    scope_set = set(scope)
    min_bal = min_balance_for_ws(cfg)
    daily_limit = float(daily_bet_ws_limit_vnd(cfg))

    funded: set[str] = set(ws_funded_account_ids(cfg, scope))
    depositing: set[str] = set()
    ws_only: set[str] = set()

    for aid in scope:
        if aid in funded:
            continue
        row = get_account(aid) or {}
        if daily_bet_today_vnd(row) >= daily_limit:
            continue
        if account_ws_deposit_busy(aid, cfg):
            depositing.add(aid)

    for aid in ws_live_in_scope(scope_set):
        if aid in funded or aid in depositing:
            continue
        row = get_account(aid) or {}
        if daily_bet_today_vnd(row) >= daily_limit:
            continue
        if account_balance_vnd(row) < min_bal:
            ws_only.add(aid)

    occupied = funded | depositing | ws_only
    return {
        "funded": funded,
        "depositing": depositing,
        "ws_only": ws_only,
        "occupied": occupied,
        "funded_n": len(funded),
        "depositing_n": len(depositing),
        "ws_only_n": len(ws_only),
        "occupied_n": len(occupied),
    }


def ws_pool_occupied_ids(cfg: dict[str, Any], scope_ids: list[str]) -> list[str]:
    """Danh sách acc đã chiếm slot (đủ tiền | đang nạp | WS chờ tiền)."""
    return sorted(ws_pool_slot_breakdown(cfg, scope_ids)["occupied"])


def format_ws_pool_slot_log(cfg: dict[str, Any], scope_ids: list[str]) -> str:
    """Một dòng log: 9 đủ tiền + 2 đang nạp + 1 WS = 12/12."""
    b = ws_pool_slot_breakdown(cfg, scope_ids)
    target = ws_account_count(cfg)
    parts = [f"{b['funded_n']} đủ tiền"]
    if b["depositing_n"]:
        parts.append(f"{b['depositing_n']} đang nạp")
    if b["ws_only_n"]:
        parts.append(f"{b['ws_only_n']} WS chờ tiền")
    return f"{' + '.join(parts)} = {b['occupied_n']}/{target}"


def ranked_ws_eligible(
    cfg: dict[str, Any], *, exclude: set[str] | None = None
) -> list[dict[str, Any]]:
    """Acc >= min và chưa đủ cap (dùng cho nạp / đếm funded)."""
    min_bal = min_balance_for_ws(cfg)
    daily_limit = float(daily_bet_ws_limit_vnd(cfg))
    ex = exclude or set()
    rows = [
        r
        for r in _pool_rows(cfg)
        if str(r.get("id") or "") not in ex
        and is_row_eligible_for_ws(r, min_bal=min_bal, daily_limit=daily_limit)
    ]
    return sort_rows_ws_fill_priority(rows, cfg)


def ranked_dang_choi_not_on_ws(
    cfg: dict[str, Any], *, exclude: set[str] | None = None
) -> list[dict[str, Any]]:
    """«Đang Chơi» + proxy, chưa có WS (không lọc balance/cap)."""
    ex = exclude or set()
    rows = [
        r
        for r in _pool_rows(cfg)
        if str(r.get("id") or "") not in ex
    ]
    return sort_rows_ws_fill_priority(rows, cfg)


def list_dang_choi_missing_ws_connect(
    cfg: dict[str, Any], *, exclude: set[str] | None = None
) -> list[str]:
    """
    «Đang Chơi» đủ tiền, chưa connect WS (có task rớt/kẹt vẫn tính thiếu WS).
    Chuẩn đầu phiên: mở lại mọi nick trong danh sách này.
    """
    ex = {str(x).strip() for x in (exclude or set()) if str(x).strip()}
    listener = pick_ws_listener_account(cfg)
    if listener:
        ex.add(listener)
    connected = {
        str(x).strip() for x in get_connected_ws_accounts() if str(x).strip()
    }
    out: list[str] = []
    for row in sort_rows_ws_fill_priority(_pool_rows(cfg), cfg):
        aid = str(row.get("id") or "").strip()
        if not aid or aid in ex:
            continue
        if aid in connected:
            continue
        if account_ws_deposit_busy(aid, cfg):
            continue
        cand = _row_fill_candidate(row, cfg, exclude=ex)
        if cand and cand[1] == "connect":
            ex.add(aid)
            out.append(aid)
    return out


def ranked_deposit_candidates(
    cfg: dict[str, Any], *, exclude: set[str] | None = None
) -> list[dict[str, Any]]:
    try:
        from xoso66_bet_assign import _auto_bet_cfg, consolidate_skips_underfunded_ws

        if consolidate_skips_underfunded_ws(_auto_bet_cfg(cfg)):
            return []
    except Exception:
        pass
    min_bal = min_balance_for_ws(cfg)
    daily_limit = float(daily_bet_ws_limit_vnd(cfg))
    ex = exclude or set()
    rows = [
        r
        for r in _pool_rows(cfg)
        if str(r.get("id") or "") not in ex
        and is_row_deposit_candidate(r, min_bal=min_bal, daily_limit=daily_limit)
    ]
    under = [r for r in rows if account_balance_vnd(r) < min_bal]
    fill_mode = ws_fill_priority_mode(cfg)
    if fill_mode == 0:
        under.sort(key=lambda r: _sort_key_ws_fill_unified(r, cfg))
    elif fill_mode == 2:
        under.sort(key=_sort_key_ws_fill_daily_desc_balance)
    else:
        under.sort(key=lambda r: _sort_key_daily_bet_ws_fill(r, cfg))
    return under


def list_ws_dang_choi_ids(
    cfg: dict[str, Any], *, allow_empty: bool = False
) -> list[str]:
    """Mọi acc «Đang Chơi» có proxy — thứ tự ưu tiên 2 đoạn."""
    status = ws_account_status(cfg)
    fixed = cfg.get("game_worker_account_ids")
    if isinstance(fixed, list):
        ids = [str(x).strip() for x in fixed if str(x).strip()]
        if ids:
            return [
                aid
                for aid in ids
                if row_allowed_for_ws(get_account(aid) or {})
            ]

    pool = _pool_rows(cfg)
    if not pool:
        if allow_empty:
            return []
        raise RuntimeError(f"Không có account status='{status}' (có proxy).")
    return [str(r["id"]) for r in sort_rows_ws_fill_priority(pool, cfg)]


def row_eligible_for_ws_fill(row: dict[str, Any], cfg: dict[str, Any]) -> bool:
    """Đủ điều kiện bù slot WS: pool «Đang Chơi» hoặc list Hết Tiền/Đủ ngày."""
    from xoso66_cf import row_cf_rate_limited

    st = str(row.get("status") or "").strip()
    if is_ws_blocked_status(st):
        return False
    if row_cf_rate_limited(row):
        return False
    if is_ws_pool_active_status(row, cfg):
        return row_allowed_for_ws(row)
    return st in WS_OPEN_LIST_STATUSES and row_allowed_for_ws_open_list(row)


def _row_fill_candidate(
    row: dict[str, Any], cfg: dict[str, Any], *, exclude: set[str]
) -> tuple[str, str] | None:
    """Trả (aid, 'connect'|'deposit') hoặc None."""
    aid = str(row.get("id") or "").strip()
    if not aid or aid in exclude:
        return None
    from xoso66_proxy import is_proxy_dead

    if is_proxy_dead(aid):
        return None
    if not row_eligible_for_ws_fill(row, cfg):
        return None
    if exceeds_ws_side_daily_cap(row, cfg):
        return None
    if account_ws_deposit_busy(aid, cfg):
        return None
    min_bal = min_balance_for_ws(cfg)
    if account_balance_vnd(row) >= min_bal:
        return aid, "connect"
    try:
        from xoso66_bet_assign import _auto_bet_cfg, consolidate_skips_underfunded_ws

        if consolidate_skips_underfunded_ws(_auto_bet_cfg(cfg)):
            return None
    except Exception:
        pass
    return aid, "deposit"


def _classify_rows_connect_deposit(
    rows: list[dict[str, Any]],
    cfg: dict[str, Any],
    *,
    exclude: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Tách nick đủ tiền (mở WS ngay) vs cần nạp (chỉ nạp, mở WS sau Hoàn tất)."""
    connect: list[str] = []
    deposit: list[str] = []
    ex = {str(x).strip() for x in (exclude or set()) if str(x).strip()}
    for row in rows:
        cand = _row_fill_candidate(row, cfg, exclude=ex)
        if not cand:
            continue
        aid, kind = cand
        ex.add(aid)
        if kind == "connect":
            connect.append(aid)
        else:
            deposit.append(aid)
    return connect, deposit


def _split_ids_connect_deposit(
    account_ids: list[str], cfg: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Tách list id đang WS / candidate: đủ tiền → connect, thiếu → deposit."""
    connect: list[str] = []
    deposit: list[str] = []
    for aid in account_ids:
        aid = str(aid).strip()
        if not aid:
            continue
        row = get_account(aid) or {}
        cand = _row_fill_candidate(row, cfg, exclude=set())
        if not cand:
            continue
        _, kind = cand
        if kind == "connect":
            connect.append(aid)
        else:
            deposit.append(aid)
    return connect, deposit


def _merge_unique_account_ids(*groups: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for group in groups:
        for aid in group:
            aid = str(aid).strip()
            if aid and aid not in seen:
                seen.add(aid)
                out.append(aid)
    return out


def _ws_fill_blocked_all_daily_cap(
    cfg: dict[str, Any],
    *,
    exclude: set[str] | None = None,
) -> bool:
    """
    True khi còn acc có proxy trong pool WS nhưng không nick nào còn chỗ cap
    (không tính nick đang nạp — chờ Hoàn tất).
    """
    ex = set(ws_slots_exclude_ids(cfg))
    if exclude:
        ex |= {str(x).strip() for x in exclude if str(x).strip()}
    daily_limit = float(daily_bet_ws_limit_vnd(cfg))
    seen: set[str] = set()
    has_account = False
    has_cap_room = False
    has_other_blocker = False

    def _scan(row: dict[str, Any]) -> None:
        nonlocal has_account, has_cap_room, has_other_blocker
        aid = str(row.get("id") or "").strip()
        if not aid or aid in ex or aid in seen:
            return
        if not str(row.get("proxy") or "").strip():
            return
        seen.add(aid)
        has_account = True
        if account_deposit_in_flight(aid, cfg):
            has_other_blocker = True
            return
        if is_row_exhausted_daily_cap(row, daily_limit=daily_limit):
            return
        has_cap_room = True

    for row in _pool_rows(cfg):
        _scan(row)
    for row in _pool_rows_ws_open_list(cfg):
        _scan(row)

    return bool(has_account and not has_cap_room and not has_other_blocker)


def _daily_bet_cap_raise_target_vnd(cfg: dict[str, Any]) -> int:
    """Mốc nâng khi hết acc còn room cap (Cửa 1 mini game ≈ 2_695_000)."""
    ab = cfg.get("auto_bet")
    if isinstance(ab, dict) and ab.get("daily_bet_cap_raise_vnd") is not None:
        return max(0, int(ab.get("daily_bet_cap_raise_vnd") or 2_695_000))
    return 2_695_000


def _daily_bet_cap_raise_enabled(cfg: dict[str, Any]) -> bool:
    """True = hết room cap → nâng daily_bet_cap_vnd; False → gán assign_strategy=2."""
    ab = cfg.get("auto_bet")
    if not isinstance(ab, dict):
        return True
    v = ab.get("daily_bet_cap_raise_enabled")
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip().lower() not in ("0", "false", "no", "off")
    return bool(v)


def _maybe_auto_raise_daily_bet_cap_on_exhausted(
    cfg: dict[str, Any],
    *,
    exclude: set[str] | None = None,
    task_n: int | None = None,
) -> bool:
    """
    Hết acc còn room theo daily_bet_cap hiện tại (còn task WS):
    - daily_bet_cap_raise_enabled=true → nâng daily_bet_cap_vnd (không đổi assign_strategy)
    - daily_bet_cap_raise_enabled=false → gán assign_strategy=2 (không đổi daily_bet_cap_vnd)
    Cooldown chung assign_strategy_switch_cooldown_sec.
    """
    ab = cfg.get("auto_bet")
    if not isinstance(ab, dict):
        return False
    if task_n is None:
        task_n = int(count_ws_slots_in_use(cfg)["task_n"])
    if int(task_n) <= 0:
        return False
    if not _assign_strategy_switch_allowed(cfg):
        return False
    if not _ws_fill_blocked_all_daily_cap(cfg, exclude=exclude):
        return False

    from xoso66_config_util import save_user_config_value

    if _daily_bet_cap_raise_enabled(cfg):
        target = _daily_bet_cap_raise_target_vnd(cfg)
        try:
            cur = int(ab.get("daily_bet_cap_vnd") or 0)
        except (TypeError, ValueError):
            cur = 0
        if cur >= target:
            return False
        if not save_user_config_value(("auto_bet", "daily_bet_cap_vnd"), target):
            return False
        ab["daily_bet_cap_vnd"] = target
        _mark_assign_strategy_switched()
        print(
            f"[CONFIG] daily_bet_cap_vnd {cur:,} → {target:,} "
            f"— hết acc còn room cap (task={task_n})",
            flush=True,
        )
        return True

    try:
        cur_s = int(ab.get("assign_strategy") or 1)
    except (TypeError, ValueError):
        cur_s = 1
    if cur_s == 2:
        return False
    if not save_user_config_value(("auto_bet", "assign_strategy"), 2):
        return False
    ab["assign_strategy"] = 2
    _mark_assign_strategy_switched()
    print(
        f"[CONFIG] assign_strategy {cur_s}→2 "
        f"— hết acc còn room cap, daily_bet_cap_raise_enabled=off (task={task_n})",
        flush=True,
    )
    return True


# Alias cũ (plan fill / gọi lịch sử).
_maybe_auto_switch_assign_strategy_on_daily_cap_exhausted = (
    _maybe_auto_raise_daily_bet_cap_on_exhausted
)


def _maybe_auto_switch_assign_strategy_when_no_ws_tasks(
    cfg: dict[str, Any],
    *,
    task_n: int | None = None,
) -> bool:
    """
    Strategy 2 nhưng không gán được → strategy 1 (cooldown):
    - pool gán cược trống (không có «Đang Chơi» trong WS, chờ WS, …), hoặc
    - không còn task WS và không pending mở slot.
    """
    ab = cfg.get("auto_bet")
    if not isinstance(ab, dict):
        return False
    if int(ab.get("assign_strategy") or 1) != 2:
        return False
    if not _assign_strategy_switch_allowed(cfg):
        return False

    from xoso66_bet_assign import is_assign_pool_empty

    pool_empty = is_assign_pool_empty(cfg)
    info = count_ws_slots_in_use(cfg)
    if task_n is None:
        task_n = int(info["task_n"])
    pending_n = int(info.get("pending_n", 0))
    no_ws_tasks = int(task_n) == 0

    if pool_empty:
        reason = "pool gán cược trống"
    elif no_ws_tasks and pending_n == 0:
        reason = "không còn task WS"
    else:
        return False

    from xoso66_config_util import save_user_config_value

    if not save_user_config_value(("auto_bet", "assign_strategy"), 1):
        return False
    ab["assign_strategy"] = 1
    _mark_assign_strategy_switched()
    print(
        f"[CONFIG] assign_strategy 2→1 — {reason} "
        f"(task={task_n}, pending={pending_n})",
        flush=True,
    )
    return True


def plan_ws_slot_fill(
    cfg: dict[str, Any],
    need: int,
    *,
    exclude: set[str] | None = None,
    prefer_dang_choi: bool = True,
    log_context: str = "",
) -> tuple[list[str], list[str]]:
    """
    Chọn tối đa `need` nick để mở WS hoặc nạp.
    prefer_dang_choi: ưu tiên pool «Đang Chơi» trước list Hết Tiền/Đủ ngày.
    """
    need = max(0, int(need))
    if need <= 0:
        return [], []

    ex = set(ws_slots_exclude_ids(cfg))
    if exclude:
        ex |= {str(x).strip() for x in exclude if str(x).strip()}
    connect_ids: list[str] = []
    deposit_ids: list[str] = []
    picked = 0

    def _take(row: dict[str, Any]) -> bool:
        nonlocal picked
        if picked >= need:
            return False
        cand = _row_fill_candidate(row, cfg, exclude=ex)
        if not cand:
            return True
        aid, kind = cand
        ex.add(aid)
        if kind == "connect":
            connect_ids.append(aid)
        else:
            deposit_ids.append(aid)
        picked += 1
        return True

    if prefer_dang_choi:
        for row in sort_rows_ws_fill_priority(_pool_rows(cfg), cfg):
            if not _take(row):
                break

    if picked < need:
        for row in iter_ws_priority_rows(cfg, exclude=ex):
            if not _take(row):
                break

    if log_context and (connect_ids or deposit_ids):
        names_c = ", ".join(username_for_log(a) for a in connect_ids[:6])
        names_d = ", ".join(username_for_log(a) for a in deposit_ids[:6])
        extra_c = f"… +{len(connect_ids) - 6}" if len(connect_ids) > 6 else ""
        extra_d = f"… +{len(deposit_ids) - 6}" if len(deposit_ids) > 6 else ""
        print(
            f"[WS-POOL] {log_context} — mở {len(connect_ids)}"
            f"{f' ({names_c}{extra_c})' if connect_ids else ''}"
            f", nạp {len(deposit_ids)}"
            f"{f' ({names_d}{extra_d})' if deposit_ids else ''}",
            flush=True,
        )
    if picked < need:
        task_n = int(count_ws_slots_in_use(cfg)["task_n"])
        if task_n > 0:
            _maybe_auto_raise_daily_bet_cap_on_exhausted(
                cfg, exclude=ex, task_n=task_n
            )
    return connect_ids, deposit_ids


def plan_priority_fill_ids(
    cfg: dict[str, Any],
    need: int,
    *,
    exclude: set[str] | None = None,
    log_context: str = "",
) -> tuple[list[str], list[str]]:
    """Lấy `need` nick từ list ưu tiên (Hết Tiền / Đủ ngày)."""
    return plan_ws_slot_fill(
        cfg,
        need,
        exclude=exclude,
        prefer_dang_choi=False,
        log_context=log_context,
    )


def deposit_ids_if_funded_shortage(
    cfg: dict[str, Any], ws_ids: list[str] | None = None
) -> list[str]:
    """Nạp khi thiếu slot (chỉ acc cần nạp trong list ưu tiên)."""
    _ = ws_ids
    need = ws_slots_need_fill(cfg, task_ids=get_ws_pool_scope_ids())
    _, deposit_ids = plan_priority_fill_ids(
        cfg, need, exclude=ws_slots_exclude_ids(cfg)
    )
    return deposit_ids


def _plan_initial_ws_connect_deposit(
    cfg: dict[str, Any], *, exclude: set[str] | None = None
) -> tuple[list[str], list[str]]:
    """Chọn tối đa ws_account_count nick: mở WS ngay + nạp trước (chỉ đủ slot)."""
    min_target = ws_account_count(cfg)
    ex = {str(x).strip() for x in (exclude or set()) if str(x).strip()}
    dc_connect, dc_deposit = _classify_rows_connect_deposit(
        sort_rows_ws_fill_priority(_pool_rows(cfg), cfg), cfg, exclude=ex
    )
    connect = [a for a in dc_connect if a not in ex][:min_target]
    need = max(0, min_target - len(connect))
    deposit: list[str] = []
    if need > 0:
        ex_set = set(connect) | ex
        deposit = [a for a in dc_deposit if a not in ex_set][:need]
        ex_set |= set(deposit)
        still = need - len(deposit)
        if still > 0:
            fc, fd = plan_ws_slot_fill(
                cfg, still, exclude=ex_set, prefer_dang_choi=False, log_context=""
            )
            connect = (connect + fc)[:min_target]
            deposit = _merge_unique_account_ids(deposit, fd)
        max_dep = max(0, min_target - len(connect))
        deposit = deposit[:max_dep]
    connect = connect[:min_target]
    deposit = [a for a in deposit if a not in set(connect)]
    return connect, deposit


def pick_ws_target_account_ids(cfg: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Mở WS chỉ nick đủ tiền; nick thiếu tiền → deposit_ids (nạp trước, mở WS sau)."""
    return _plan_initial_ws_connect_deposit(cfg)


def prune_ws_target_at_round_start(
    cfg: dict[str, Any], account_ids: list[str]
) -> tuple[list[str], list[str]]:
    """Tương thích: lọc list theo điều kiện đầu phiên (không dùng cho prune task)."""
    removed = accounts_to_disconnect_at_round_start(cfg, account_ids)
    kept = [a for a in account_ids if a not in removed]
    return kept, removed


@dataclass
class WsSyncPlan:
    connect_all: list[str]
    target: list[str]
    deposit_ids: list[str]
    prune_removed: list[str]
    fill_connect_ids: list[str]


def build_ws_sync_plan(
    cfg: dict[str, Any],
    current_ids: list[str],
    *,
    round_start: bool = False,
    ws_task_ids: list[str] | None = None,
    just_evicted: list[str] | None = None,
) -> WsSyncPlan | None:
    """Kế hoạch sync WS — ủy quyền orchestrator 3 cổng."""
    return reconcile_ws_pool(
        cfg,
        current_ids,
        round_start=round_start,
        ws_task_ids=ws_task_ids,
        just_evicted=just_evicted,
    )


def reconcile_ws_pool(
    cfg: dict[str, Any],
    current_ids: list[str],
    *,
    round_start: bool = False,
    ws_task_ids: list[str] | None = None,
    just_evicted: list[str] | None = None,
) -> WsSyncPlan | None:
    """
    Phiên mới / Resync — đúng 5 việc, chạy song song, mỗi việc tự đọc state:

    1) DB < min, ∉ C → Hết Tiền
    2) đủ cap cược ngày → Đủ ngày
    3) bù Đang Chơi (trừ D)
    4) Đang Chơi chưa có WS → mở WS
    5) có WS nhưng không Đang Chơi (∉ C) → đóng WS

    Chỉ log khi có thay đổi. Luồng chia cược độc lập.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    _ = current_ids, just_evicted
    _reconcile_begin()

    ctx = "Phiên mới" if round_start else "Resync"
    min_target = ws_account_count(cfg)
    min_bal = min_balance_for_ws(cfg)
    daily_limit = float(daily_bet_ws_limit_vnd(cfg))
    lid = pick_ws_listener_account(cfg)
    task_keys = [
        str(x).strip()
        for x in (ws_task_ids if ws_task_ids is not None else get_ws_task_accounts())
        if str(x).strip()
    ]

    opened: list[str] = []
    closed: list[str] = []
    deposit_ids: list[str] = []
    lock = threading.Lock()

    def _names(ids: list[str], *, n: int = 16) -> str:
        if not ids:
            return "(không)"
        shown = ", ".join(username_for_log(a) for a in ids[:n])
        if len(ids) > n:
            shown += f" … +{len(ids) - n}"
        return shown

    def _list_A() -> set[str]:
        s = {
            str(x).strip() for x in get_connected_ws_accounts() if str(x).strip()
        }
        s |= get_pending_ws_slot_ids()
        if lid:
            s.discard(lid)
        return s

    def viec1_het_tien() -> None:
        C = list_C_pending_kq_ids()
        dang = dang_choi_account_ids(cfg)
        A = _list_A()
        done: list[str] = []
        for aid in sorted(dang | A):
            if aid in C or (lid and aid == lid):
                continue
            if is_win_credit_recheck_pending(aid):
                continue
            row = get_account(aid) or {}
            if not is_balance_too_low_for_ws(row, cfg):
                continue
            if apply_account_status_intent(
                aid, STATUS_HET_TIEN, reason=f"{ctx} việc1 DB < {min_bal:,}"
            ):
                done.append(aid)
        if done:
            print(
                f"[WS-POOL] {ctx} việc1 — Hết Tiền: {_names(done)}",
                flush=True,
            )

    def viec2_du_ngay() -> None:
        C = list_C_pending_kq_ids()
        done: list[str] = []
        for aid in sorted(dang_choi_account_ids(cfg)):
            if aid in C:
                continue
            row = get_account(aid) or {}
            if not is_row_exhausted_daily_cap(row, daily_limit=daily_limit):
                continue
            if apply_account_status_intent(
                aid, STATUS_DU_NGAY, reason=f"{ctx} việc2 đủ cap cược ngày"
            ):
                done.append(aid)
        if done:
            print(
                f"[WS-POOL] {ctx} việc2 — Đủ ngày: {_names(done)}",
                flush=True,
            )

    def viec3_bu_dang_choi() -> None:
        dang = dang_choi_account_ids(cfg)
        D = list_D_deposit_ids(cfg)
        need = max(0, min_target - len(dang | D))
        if need <= 0:
            return
        exclude = set(dang) | set(D) | ws_slots_exclude_ids(cfg)
        fill_connect, fill_deposit = plan_ws_slot_fill(
            cfg, need, exclude=exclude, prefer_dang_choi=False, log_context=""
        )
        if not fill_connect and not fill_deposit:
            fill_connect, fill_deposit = plan_priority_fill_ids(
                cfg, need, exclude=exclude, log_context=""
            )
        done: list[str] = []
        for aid in fill_connect:
            if len(done) >= need:
                break
            if apply_account_status_intent(
                aid, STATUS_DANG_CHOI, reason=f"{ctx} việc3 bù Đang Chơi"
            ):
                done.append(aid)
        dep = list(fill_deposit)
        try:
            from xoso66_bet_assign import (
                _auto_bet_cfg,
                consolidate_skips_underfunded_ws,
            )

            if consolidate_skips_underfunded_ws(_auto_bet_cfg(cfg)):
                dep = []
        except Exception:
            pass
        with lock:
            deposit_ids.extend(dep)
        if done:
            print(
                f"[WS-POOL] {ctx} việc3 — bù Đang Chơi: {_names(done)}",
                flush=True,
            )
        if dep:
            print(
                f"[WS-POOL] {ctx} việc3 — xếp nạp D: {_names(dep)}",
                flush=True,
            )

    def viec4_mo_ws() -> None:
        from xoso66_proxy import is_proxy_dead

        C = list_C_pending_kq_ids()
        dang = dang_choi_account_ids(cfg)
        A = _list_A()
        need = [
            a
            for a in sorted(dang)
            if a not in A and a not in C and not is_proxy_dead(a)
        ]
        room = max(0, min_target - len(A))
        need = need[:room]
        if not need:
            return
        out = request_ws_open(
            need,
            reason=f"{ctx} việc4",
            cfg=cfg,
            ensure_dang_choi=False,
            quiet=True,
        )
        with lock:
            opened.extend(out)
        if out:
            print(
                f"[WS-POOL] {ctx} việc4 — mở WS: {_names(out)}",
                flush=True,
            )

    def viec5_dong_ws() -> None:
        C = list_C_pending_kq_ids()
        dang = dang_choi_account_ids(cfg)
        A = _list_A()
        need = [a for a in sorted(A) if a not in dang and a not in C]
        for aid in task_keys:
            if lid and aid == lid:
                continue
            if aid in C or aid in dang or aid in need:
                continue
            need.append(aid)
        if not need:
            return
        out = request_ws_close(
            need,
            reason=f"{ctx} việc5",
            flow=f"{ctx} việc5",
            cfg=cfg,
        )
        with lock:
            closed.extend(out)

    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = [
            ex.submit(viec1_het_tien),
            ex.submit(viec2_du_ngay),
            ex.submit(viec3_bu_dang_choi),
            ex.submit(viec4_mo_ws),
            ex.submit(viec5_dong_ws),
        ]
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception as e:
                print(f"[WS-POOL] {ctx} lỗi: {e}", flush=True)

    dang = dang_choi_account_ids(cfg)
    C = list_C_pending_kq_ids()
    keep_pending = [a for a in task_keys if a in C]
    target = filter_ws_target_connectable(
        cfg,
        sorted(set(dang) | set(opened) | set(keep_pending)),
        ws_task_ids=task_keys,
    )
    task_set = set(task_keys)
    connected = {
        str(x).strip() for x in get_connected_ws_accounts() if str(x).strip()
    }
    connect_new = [
        a for a in opened if a not in task_set or a not in connected
    ]

    if (
        not round_start
        and not deposit_ids
        and not closed
        and not opened
        and set(task_keys) == set(target)
    ):
        return None

    return WsSyncPlan(
        connect_all=connect_new,
        target=target,
        deposit_ids=list(dict.fromkeys(deposit_ids)),
        prune_removed=closed,
        fill_connect_ids=connect_new,
    )



def _filter_ids_for_ws_deposit_schedule(
    cfg: dict[str, Any], account_ids: list[str]
) -> list[str]:
    """Bỏ nick đang nạp / DB đã đủ min_balance — tránh lên lịch nạp trùng."""
    try:
        from xoso66_bet_assign import _auto_bet_cfg, consolidate_skips_underfunded_ws

        if consolidate_skips_underfunded_ws(_auto_bet_cfg(cfg)):
            return []
    except Exception:
        pass
    min_bal = min_balance_for_ws(cfg)
    out: list[str] = []
    for aid in account_ids:
        aid = str(aid).strip()
        if not aid:
            continue
        if account_deposit_in_flight(aid, cfg):
            continue
        if account_balance_vnd(get_account(aid) or {}) >= min_bal:
            continue
        out.append(aid)
    return out


def is_ws_low_balance_error(exc: BaseException | None) -> bool:
    """Lỗi mở WS do prep/balance thấp — không respawn, chuyển sang nạp."""
    if exc is None:
        return False
    s = str(exc).lower()
    return "số dư dưới ngưỡng" in s or "prep session/balance" in s


def schedule_fund_deposit_for_ws_shortage(
    cfg: dict[str, Any], deposit_ids: list[str], *, label: str = ""
) -> None:
    """Nạp nền — không chặn asyncio / BẮT ĐẦU PHIÊN."""
    from xoso66_shutdown import stopping

    if stopping():
        return
    raw = [str(x).strip() for x in deposit_ids if str(x).strip()]
    ids = _filter_ids_for_ws_deposit_schedule(cfg, raw)
    if raw and len(ids) < len(raw):
        skipped = [a for a in raw if a not in set(ids)]
        names = ", ".join(username_for_log(a) for a in skipped[:8])
        extra = f"… +{len(skipped) - 8}" if len(skipped) > 8 else ""
        print(
            f"[WS-POOL] Bỏ lên lịch nạp (đủ tiền / đang có lệnh): {names}{extra}",
            flush=True,
        )
    need = ws_slots_need_fill(cfg, task_ids=get_ws_task_accounts())
    if need <= 0 and ids:
        occupied = ws_slots_exclude_ids(cfg)
        min_bal = min_balance_for_ws(cfg)
        connected_live = {
            str(x).strip() for x in get_connected_ws_accounts() if str(x).strip()
        }
        capped: list[str] = []
        skipped_pool_full: list[str] = []
        for aid in ids:
            if aid in occupied:
                capped.append(aid)
                continue
            row = get_account(aid) or {}
            # Thiếu tiền + chưa connect: vẫn nạp (task rớt không chiếm slot thật).
            if aid not in connected_live and account_balance_vnd(row) < min_bal:
                capped.append(aid)
                continue
            skipped_pool_full.append(aid)
        if skipped_pool_full:
            names = ", ".join(username_for_log(a) for a in skipped_pool_full[:8])
            extra = (
                f"… +{len(skipped_pool_full) - 8}"
                if len(skipped_pool_full) > 8
                else ""
            )
            print(
                f"[WS-POOL] Bỏ lên lịch nạp (pool WS đầy): {names}{extra}",
                flush=True,
            )
        ids = capped
    if not ids:
        return
    with _ws_deposit_scheduled_lock:
        batch: list[str] = []
        deferred: list[str] = []
        for aid in ids:
            if aid in _ws_deposit_scheduled:
                deferred.append(aid)
                continue
            _ws_deposit_scheduled.add(aid)
            batch.append(aid)
        ids = batch
    if deferred:
        names = ", ".join(username_for_log(a) for a in deferred[:8])
        extra = f"… +{len(deferred) - 8}" if len(deferred) > 8 else ""
        print(
            f"[WS-POOL] Bỏ lên lịch nạp (luồng nạp đang chạy): {names}{extra}",
            flush=True,
        )
    if not ids:
        return
    mark_pending_ws_slots(ids)
    tag = label or "ws-pool-deposit"

    def _run() -> None:
        try:
            fund_deposit_for_ws_shortage(cfg, ids)
        except Exception as e:
            from xoso66_config_util import main_progress

            main_progress(f"[WS-POOL] Nạp ({tag}): {e}")
        finally:
            with _ws_deposit_scheduled_lock:
                for aid in ids:
                    _ws_deposit_scheduled.discard(aid)
            for aid in ids:
                if account_deposit_in_flight(aid, cfg):
                    continue
                clear_pending_ws_slot(aid)

    threading.Thread(target=_run, name=tag, daemon=True).start()


def fund_deposit_for_ws_shortage(
    cfg: dict[str, Any], deposit_ids: list[str]
) -> list[str]:
    """Nạp acc đã chọn (balance < min) — poll Hoàn tất rồi Đang Chơi + mở WS."""
    from xoso66_shutdown import stopping

    if stopping():
        return []
    ids = _filter_ids_for_ws_deposit_schedule(
        cfg, [str(x).strip() for x in deposit_ids if str(x).strip()]
    )
    if not ids:
        return []
    min_bal = min_balance_for_ws(cfg)
    min_target = ws_account_count(cfg)
    scope = list_ws_dang_choi_ids(cfg, allow_empty=True) or [
        str(x).strip() for x in deposit_ids if str(x).strip()
    ]
    print(
        f"[WS-POOL] Slot {format_ws_pool_slot_log(cfg, scope)} — nạp "
        f"{', '.join(username_for_log(a) for a in ids)} "
        f"(poll Hoàn tất → Đang Chơi + mở WS)",
        flush=True,
    )
    rep = fund_accounts_below_minimum(
        cfg, ids, priority_ids=ids, wait_confirm=True, open_ws_on_confirm=True
    )
    return list(rep.get("deposited") or [])


def _log_ws_connect_from_balance_pool(
    cfg: dict[str, Any], rows: list[dict[str, Any]]
) -> None:
    if not rows:
        return
    min_bal = min_balance_for_ws(cfg)
    names = ", ".join(
        username_for_log(str(r.get("id") or ""), r) for r in rows
    )
    print(
        f"[WS-POOL] Thiếu acc >= {min_bal:,} — kết nối WS (balance cao trước): "
        f"{names}",
        flush=True,
    )


def ws_pool_sync_target(
    cfg: dict[str, Any], current_ids: list[str]
) -> tuple[list[str], list[str]] | None:
    """Tương thích cũ — dùng build_ws_sync_plan khi cần round_start."""
    plan = build_ws_sync_plan(cfg, current_ids, round_start=False)
    if plan is None:
        return None
    off_ws = ranked_dang_choi_not_on_ws(cfg, exclude=set(current_ids))
    if off_ws:
        _log_ws_connect_from_balance_pool(cfg, off_ws)
    return plan.target, plan.deposit_ids


def clear_ws_pool_cache() -> None:
    global _pool_cache, _selection_logged
    with _pool_cache_lock:
        _pool_cache = []
    _selection_logged = False


def pick_ws_account_ids(cfg: dict[str, Any]) -> list[str]:
    """Chọn lại pool WS theo config hiện tại (bỏ cache)."""
    return resolve_ws_account_ids(cfg, force=True)


def min_balance_for_ws(cfg: dict[str, Any]) -> int:
    """
    Ngưỡng đủ tiền WS.
    Strategy 3: auto_bet.consolidate_min_ws_balance_vnd nếu > 0 (mặc định 50k).
    Còn lại: game_worker.min_balance_vnd override, không thì bet_step_vnd.
    """
    try:
        from xoso66_bet_assign import (
            _auto_bet_cfg,
            consolidate_min_ws_balance_vnd,
            is_assign_strategy_3,
        )

        acfg = _auto_bet_cfg(cfg)
        if is_assign_strategy_3(acfg):
            floor = consolidate_min_ws_balance_vnd(acfg)
            if floor > 0:
                return floor
    except Exception:
        pass
    gw = game_worker_cfg(cfg)
    if gw.get("min_balance_vnd") is not None:
        return int(gw.get("min_balance_vnd") or bet_step_vnd(cfg))
    return bet_step_vnd(cfg)


def side_total_vnd(
    cfg: dict[str, Any],
    jackpot_vnd: float | None = None,
    *,
    game_id: int | None = None,
) -> int:
    from xoso66_jackpot_picker import resolve_side_total_vnd

    return resolve_side_total_vnd(cfg, jackpot_vnd, game_id=game_id)


def exceeds_ws_daily_cap(row: dict[str, Any], cfg: dict[str, Any]) -> bool:
    """True nếu không còn chỗ cho thêm một lệnh bet_step (theo daily_bet_cap_vnd)."""
    return daily_bet_today_vnd(row) + bet_step_vnd(cfg) > daily_bet_cap_vnd(cfg)


def exceeds_ws_side_daily_cap(row: dict[str, Any], cfg: dict[str, Any]) -> bool:
    """Alias cũ — dùng daily_bet_cap, không phải side_total."""
    return exceeds_ws_daily_cap(row, cfg)


def ws_account_status(cfg: dict[str, Any]) -> str:
    gw = game_worker_cfg(cfg)
    return str(gw.get("account_status") or STATUS_DANG_CHOI).strip() or STATUS_DANG_CHOI


def daily_bet_cap_vnd(cfg: dict[str, Any]) -> int:
    ab = cfg.get("auto_bet")
    if isinstance(ab, dict) and ab.get("daily_bet_cap_vnd") is not None:
        return int(ab.get("daily_bet_cap_vnd") or 890_000)
    gw = game_worker_cfg(cfg)
    return int(gw.get("daily_bet_cap_vnd") or 890_000)


def bet_step_vnd(cfg: dict[str, Any]) -> int:
    ab = cfg.get("auto_bet")
    if isinstance(ab, dict) and ab.get("bet_step_vnd"):
        return int(ab.get("bet_step_vnd") or 10_000)
    return 10_000


def daily_bet_ws_limit_vnd(cfg: dict[str, Any]) -> int:
    """
    Cược ngày tối đa để còn đặt được lệnh bet_step (daily_bet_cap - step).
    daily >= limit → coi đủ cap WS / Đủ ngày.
    """
    return max(0, daily_bet_cap_vnd(cfg) - bet_step_vnd(cfg))


def daily_bet_today_vnd(row: dict[str, Any]) -> float:
    return _daily_bet_from_row(row)


def account_balance_vnd(row: dict[str, Any]) -> float:
    return float(row.get("balance") or 0)


def sync_live_balance_vnd(account_id: str) -> tuple[float | None, float, str]:
    """
    GET getBalance trên site (cập nhật DB).
    Trả (số dư live, số dư DB trước khi gọi, lỗi nếu fail).
    """
    aid = str(account_id).strip()
    row = get_account(aid) or {}
    db_before = account_balance_vnd(row)
    from xoso66_session import refresh_account_balance_to_db

    try:
        rep = refresh_account_balance_to_db(aid, force_relogin=False)
    except Exception as e:
        try:
            from xoso66_proxy import (
                maybe_report_proxy_dead_from_exception,
                resolve_proxy,
            )

            maybe_report_proxy_dead_from_exception(
                aid, e, proxy_str=resolve_proxy(row), source="getBalance"
            )
        except Exception:
            pass
        return None, db_before, str(e)
    if not rep.get("ok"):
        err = str(rep.get("error") or "getBalance thất bại")
        try:
            from xoso66_proxy import (
                maybe_report_proxy_dead_from_message,
                resolve_proxy,
            )

            maybe_report_proxy_dead_from_message(
                aid, err, proxy_str=resolve_proxy(row), source="getBalance"
            )
        except Exception:
            pass
        return None, db_before, err
    return float(rep.get("balance") or 0), db_before, ""


def _balance_vnd_after_site_refresh(account_id: str) -> tuple[float | None, float, bool]:
    """
    Gọi getBalance → cập nhật DB.
    Trả (số dư sau refresh, DB trước, refresh_ok).
    """
    aid = str(account_id).strip()
    row = get_account(aid) or {}
    db_before = account_balance_vnd(row)
    try:
        from xoso66_session import ensure_session, refresh_account_balance_to_db

        session = ensure_session(aid, force_login=False)
        rep = refresh_account_balance_to_db(aid, session, refresh=True, force_relogin=False)
    except Exception:
        return None, db_before, False
    if not rep.get("ok"):
        return None, db_before, False
    try:
        live = float(rep.get("balance") if rep.get("balance") is not None else 0)
    except (TypeError, ValueError):
        live = account_balance_vnd(get_account(aid) or {})
    return live, db_before, True


def restore_funded_ws_pool_accounts(
    cfg: dict[str, Any], account_ids: list[str], *, log: bool = True
) -> list[str]:
    """DB/site đủ tiền — không nạp; về Đang Chơi + resync bù WS."""
    min_bal = min_balance_for_ws(cfg)
    funded: list[str] = []
    for aid in account_ids:
        aid = str(aid).strip()
        if not aid:
            continue
        if account_balance_vnd(get_account(aid) or {}) >= min_bal:
            funded.append(aid)
    if not funded:
        return []
    if log:
        names = ", ".join(username_for_log(a) for a in funded[:8])
        extra = f"… +{len(funded) - 8}" if len(funded) > 8 else ""
        print(
            f"[WS-POOL] Đủ tiền — không nạp, set Đang Chơi + mở WS: "
            f"{names}{extra}",
            flush=True,
        )
    open_ws_after_deposit_confirmed(funded, cfg)
    return funded


def account_needs_deposit_live(
    account_id: str,
    min_bal: int,
    *,
    log: bool = True,
) -> bool:
    """True nếu sau getBalance thực tế vẫn < min_bal (tránh nạp khi DB cũ = 0)."""
    user = username_for_log(account_id)
    try:
        from xoso66_proxy import is_proxy_dead

        if is_proxy_dead(account_id):
            if log:
                print(
                    f"[WS-POOL] {user} bỏ nạp — proxy chết / Lỗi proxy",
                    flush=True,
                )
            return False
    except Exception:
        pass
    live, db, err = sync_live_balance_vnd(account_id)
    if live is None:
        if log:
            print(
                f"[WS-POOL] {user} không getBalance ({err}) — "
                f"coi theo DB {db:,.0f}",
                flush=True,
            )
        try:
            from xoso66_proxy import is_proxy_dead, is_proxy_error_message

            # Proxy chết: không fallback DB để spam nạp.
            if is_proxy_dead(account_id) or is_proxy_error_message(err):
                if log:
                    print(
                        f"[WS-POOL] {user} bỏ nạp — lỗi proxy (getBalance)",
                        flush=True,
                    )
                return False
        except Exception:
            pass
        return db < min_bal
    if live >= min_bal:
        if log and db < min_bal:
            print(
                f"[WS-POOL] {user} DB {db:,.0f} < {min_bal:,} "
                f"nhưng site {live:,.0f} — bỏ nạp",
                flush=True,
            )
        return False
    if log:
        print(
            f"[WS-POOL] {user} site {live:,.0f} < {min_bal:,} "
            f"(DB {db:,.0f}) — nạp",
            flush=True,
        )
    return True


def mark_pool_startup_done() -> None:
    global _pool_startup_done
    with _pool_startup_lock:
        _pool_startup_done = True


def is_pool_startup_done() -> bool:
    with _pool_startup_lock:
        return _pool_startup_done


def enable_ws_round_sync() -> None:
    """Bật resync đầu phiên sau khi worker đã apply pool lần đầu (tránh trùng lúc khởi động)."""
    global _ws_round_sync_enabled
    with _ws_round_sync_lock:
        _ws_round_sync_enabled = True


def ws_round_sync_enabled() -> bool:
    with _ws_round_sync_lock:
        return _ws_round_sync_enabled


def ensure_pool_startup_before_deposit(
    cfg: dict[str, Any], account_ids: list[str]
) -> None:
    """Bắt buộc balance + user_token trước auto nạp (main đã chạy thì bỏ qua)."""
    if is_pool_startup_done():
        return
    from xoso66_config_util import startup_async_enabled

    if startup_async_enabled(cfg):
        # Main đã check nền mọi «Đang Chơi» — không chặn mở WS chờ 1 acc chậm.
        mark_pool_startup_done()
        return
    from xoso66_startup_checks import run_startup_checks_for_pool

    run_startup_checks_for_pool(cfg, account_ids)
    mark_pool_startup_done()


def register_ws_connected(
    account_id: str, *, conn_gen: str | None = None
) -> None:
    aid = str(account_id or "").strip()
    if not aid:
        return
    from xoso66_proxy import clear_proxy_dead

    clear_proxy_dead(aid)
    clear_pending_ws_slot(aid)
    with _connected_ws_lock:
        _connected_ws_ids.add(aid)
        if conn_gen:
            _connected_ws_gens[aid] = str(conn_gen)


def mark_ws_connect_failed(
    account_id: str, *, reason: str = "", exc: BaseException | None = None
) -> None:
    """Proxy/WS lỗi — báo proxy chết và bỏ bù WS."""
    from xoso66_proxy import (
        is_proxy_transport_error,
        maybe_report_proxy_dead_from_exception,
        report_proxy_dead,
    )

    aid = str(account_id or "").strip()
    if not aid:
        return
    err = str(reason or "").lower()
    if "generatorexit" in err or "destroyed but it is pending" in err:
        return
    try:
        from xoso66_shutdown import stopping

        if stopping():
            return
    except Exception:
        pass
    if exc is not None and maybe_report_proxy_dead_from_exception(
        aid, exc, source="WS"
    ):
        return
    if exc is not None and "generatorexit" in str(exc).lower():
        return
    if err and any(
        m in err
        for m in ("proxy", "socks", "timeout", "timed out", "connection refused")
    ):
        report_proxy_dead(aid, source="WS", detail=reason[:160] or "connect fail")
        return
    from xoso66_accounts_db import username_for_log

    print(
        f"[WS-POOL] Bỏ bù WS {username_for_log(aid)} — {reason or 'connect fail'}",
        flush=True,
    )


def unregister_ws_connected(
    account_id: str, *, conn_gen: str | None = None
) -> None:
    """
    Bỏ khỏi connected set.
    Nếu truyền conn_gen: chỉ bỏ khi khớp (task cũ không xóa socket mới).
    """
    aid = str(account_id or "").strip()
    if not aid:
        return
    with _connected_ws_lock:
        if conn_gen is not None:
            cur = _connected_ws_gens.get(aid)
            if cur is not None and cur != str(conn_gen):
                return
            if cur == str(conn_gen):
                _connected_ws_gens.pop(aid, None)
        else:
            _connected_ws_gens.pop(aid, None)
        _connected_ws_ids.discard(aid)


def clear_all_ws_connected() -> None:
    """Ctrl+C / shutdown — tránh pool nghĩ nick vẫn đang connect."""
    with _connected_ws_lock:
        _connected_ws_ids.clear()
        _connected_ws_gens.clear()


def reset_ws_listener_cache() -> None:
    """Xóa cache listener — dùng trước khi mở lại WS sau crash."""
    global _ws_listener_id
    with _ws_listener_lock:
        _ws_listener_id = None


def clear_all_pending_ws_slots() -> None:
    """Xóa hết pending mở WS (không đụng hàng nạp deposit)."""
    with _pending_slot_lock:
        _pending_slot_ids.clear()


def reset_ws_pool_runtime_state() -> None:
    """
    Dọn state RAM stale sau crash WS worker (trước restart).
    Clear pending-open — nếu giữ pending, soft-restart bị filter chặn spawn
    (giữ ~14 nick nhưng chỉ mở được 1).
    Không đụng DB / deposit cache / hàng nạp.
    """
    clear_all_ws_connected()
    clear_all_pending_ws_slots()
    reset_ws_listener_cache()
    register_ws_task_ids_provider(None)


def get_connected_ws_accounts() -> list[str]:
    """Nick WS đã connect thành công (dùng cho gán cược — không chờ cả pool)."""
    with _connected_ws_lock:
        return sorted(_connected_ws_ids)


def ws_fill_priority_mode(cfg: dict[str, Any]) -> int:
    """0 / 1 / 2 — xem sort_rows_ws_fill_priority; giá trị khác → 1."""
    gw = game_worker_cfg(cfg)
    try:
        mode = int(gw.get("ws_fill_priority", 1))
    except (TypeError, ValueError):
        mode = 1
    return mode if mode in (0, 1, 2) else 1


def _sort_key_balance_ws_fill(r: dict[str, Any], cfg: dict[str, Any]) -> tuple:
    bal = account_balance_vnd(r)
    user = str(r.get("username") or "")
    aid = str(r.get("id") or "")
    if ws_fill_priority_mode(cfg) == 0:
        return (bal, user, aid)
    return (-bal, user, aid)


def _sort_key_daily_bet_ws_fill(r: dict[str, Any], cfg: dict[str, Any]) -> tuple:
    daily = daily_bet_today_vnd(r)
    user = str(r.get("username") or "")
    aid = str(r.get("id") or "")
    if ws_fill_priority_mode(cfg) == 0:
        return (-daily, user, aid)
    return (daily, user, aid)


def _assign_strategy_from_cfg(cfg: dict[str, Any]) -> int:
    ab = cfg.get("auto_bet") if isinstance(cfg.get("auto_bet"), dict) else {}
    try:
        s = int(ab.get("assign_strategy") or 1)
    except (TypeError, ValueError):
        s = 1
    return 2 if s == 2 else 1


def _sort_key_daily_bet_then_balance(r: dict[str, Any]) -> tuple:
    """Chiến lược 2: cược ngày thấp → cao; bằng nhau balance cao trước."""
    return (
        daily_bet_today_vnd(r),
        -account_balance_vnd(r),
        str(r.get("username") or ""),
        str(r.get("id") or ""),
    )


def _sort_key_daily_bet_desc_then_balance(r: dict[str, Any]) -> tuple:
    """Chiến lược 1: cược ngày cao → thấp; bằng nhau balance cao trước."""
    return (
        -daily_bet_today_vnd(r),
        -account_balance_vnd(r),
        str(r.get("username") or ""),
        str(r.get("id") or ""),
    )


def _sort_key_ws_fill_unified(r: dict[str, Any], cfg: dict[str, Any]) -> tuple:
    """ws_fill_priority=0: balance↑ rồi cược ngày↓ — mọi acc, không tách ngưỡng min."""
    bal = account_balance_vnd(r)
    daily = daily_bet_today_vnd(r)
    user = str(r.get("username") or "")
    aid = str(r.get("id") or "")
    return (bal, -daily, user, aid)


def _sort_key_ws_fill_daily_desc_balance(r: dict[str, Any]) -> tuple:
    """ws_fill_priority=2: cược ngày↓ rồi số dư↓ — mọi acc, không tách ngưỡng min."""
    return (
        -daily_bet_today_vnd(r),
        -account_balance_vnd(r),
        str(r.get("username") or ""),
        str(r.get("id") or ""),
    )


def sort_rows_ws_fill_priority(
    rows: list[dict[str, Any]], cfg: dict[str, Any]
) -> list[dict[str, Any]]:
    """
    Bù slot WS / mở WS / nạp (game_worker.ws_fill_priority):
      ws_fill_priority=0: balance thấp→cao (mọi acc), bằng nhau cược ngày cao→thấp.
      ws_fill_priority=1: đủ tiền balance cao→thấp; thiếu tiền cược ngày thấp→cao.
      ws_fill_priority=2: cược ngày cao→thấp (mọi acc), bằng nhau số dư cao→thấp.
    Strategy 3: luôn balance cao→thấp (funded trước), bỏ qua ws_fill_priority 0/2.
    """
    force_s3_desc = False
    try:
        from xoso66_bet_assign import _auto_bet_cfg, is_assign_strategy_3

        force_s3_desc = is_assign_strategy_3(_auto_bet_cfg(cfg))
    except Exception:
        force_s3_desc = False

    fill_mode = ws_fill_priority_mode(cfg)
    if not force_s3_desc and fill_mode == 0:
        out = list(rows)
        out.sort(key=lambda r: _sort_key_ws_fill_unified(r, cfg))
        return out
    if not force_s3_desc and fill_mode == 2:
        out = list(rows)
        out.sort(key=_sort_key_ws_fill_daily_desc_balance)
        return out

    min_bal = min_balance_for_ws(cfg)
    funded: list[dict[str, Any]] = []
    under: list[dict[str, Any]] = []
    for row in rows:
        if account_balance_vnd(row) >= min_bal:
            funded.append(row)
        else:
            under.append(row)
    if force_s3_desc:
        funded.sort(
            key=lambda r: (
                -account_balance_vnd(r),
                str(r.get("username") or ""),
                str(r.get("id") or ""),
            )
        )
        under.sort(
            key=lambda r: (
                -account_balance_vnd(r),
                str(r.get("username") or ""),
                str(r.get("id") or ""),
            )
        )
    else:
        funded.sort(key=lambda r: _sort_key_balance_ws_fill(r, cfg))
        under.sort(key=lambda r: _sort_key_daily_bet_ws_fill(r, cfg))
    return funded + under


def sort_rows_ws_priority(
    rows: list[dict[str, Any]], cfg: dict[str, Any]
) -> list[dict[str, Any]]:
    """
    (Legacy / gán cược) Chiến lược 1: cược ngày cao → thấp (bằng nhau: balance cao).
    Chiến lược 2: cược ngày thấp → cao (bằng nhau: balance cao).
    WS pool dùng sort_rows_ws_fill_priority.
    """
    out = list(rows)
    if _assign_strategy_from_cfg(cfg) == 2:
        out.sort(key=_sort_key_daily_bet_then_balance)
    else:
        out.sort(key=_sort_key_daily_bet_desc_then_balance)
    return out


def iter_ws_priority_rows(
    cfg: dict[str, Any], *, exclude: set[str] | None = None
) -> list[dict[str, Any]]:
    """List ưu tiên mở WS / nạp: Hết Tiền / Đủ ngày — theo game_worker.ws_fill_priority."""
    ex = exclude or set()
    rows = [
        r
        for r in _pool_rows_ws_open_list(cfg)
        if str(r.get("id") or "") and str(r.get("id") or "") not in ex
    ]
    return sort_rows_ws_fill_priority(rows, cfg)


def list_ws_priority_account_ids(
    cfg: dict[str, Any], *, exclude: set[str] | None = None
) -> list[str]:
    return [str(r["id"]) for r in iter_ws_priority_rows(cfg, exclude=exclude)]


def list_ws_priority_accounts_payload(cfg: dict[str, Any]) -> dict[str, Any]:
    """API: danh sách ưu tiên mở WS / nạp (theo game_worker.ws_fill_priority)."""
    step = bet_step_vnd(cfg)
    rows = iter_ws_priority_rows(cfg)
    items: list[dict[str, Any]] = []
    for r in rows:
        aid = str(r.get("id") or "")
        items.append(
            {
                "id": aid,
                "username": str(r.get("username") or ""),
                "status": str(r.get("status") or ""),
                "balance": account_balance_vnd(r),
                "daily_bet_today": daily_bet_today_vnd(r),
                "segment": "funded" if account_balance_vnd(r) >= step else "underfunded",
            }
        )
    return {
        "bet_step_vnd": step,
        "daily_bet_cap_vnd": daily_bet_cap_vnd(cfg),
        "side_total_vnd": side_total_vnd(cfg),
        "ws_account_count": ws_account_count(cfg),
        "ws_fill_priority": ws_fill_priority_mode(cfg),
        "allowed_statuses": sorted(WS_OPEN_LIST_STATUSES),
        "blocked_statuses": sorted(WS_BLOCKED_STATUSES),
        **ws_target_occupied_counts(cfg),
        "account_ids": [x["id"] for x in items],
        "accounts": items,
    }


def round_start_balance_check_delay_sec(cfg: dict[str, Any]) -> float:
    gw = game_worker_cfg(cfg)
    if "round_start_balance_check_delay_sec" in gw:
        return max(0.0, float(gw.get("round_start_balance_check_delay_sec") or 0))
    return 20.0


def win_credit_recheck_delays_sec(cfg: dict[str, Any]) -> list[float]:
    """
    Mốc giây (từ lúc KQ) để refresh balance nick thắng còn DB < min.
    Mặc định 10,20,30,40,50 — tối đa 5 lần.
    """
    gw = game_worker_cfg(cfg)
    raw = gw.get("win_credit_recheck_delays_sec")
    out: list[float] = []
    if isinstance(raw, (list, tuple)):
        for x in raw:
            try:
                d = float(x)
            except (TypeError, ValueError):
                continue
            if d >= 0:
                out.append(d)
    if not out:
        out = [10.0, 20.0, 30.0, 40.0, 50.0]
    out = sorted({float(d) for d in out})
    return out[:5]


def is_win_credit_recheck_pending(account_id: str) -> bool:
    aid = str(account_id).strip()
    if not aid:
        return False
    with _win_credit_recheck_lock:
        return aid in _win_credit_recheck_since


def mark_win_credit_recheck(account_ids: list[str]) -> list[str]:
    """Đánh dấu nick đang chờ thưởng thắng; trả list mới (chưa có timer)."""
    now = time.time()
    fresh: list[str] = []
    with _win_credit_recheck_lock:
        for aid in account_ids:
            aid = str(aid).strip()
            if not aid:
                continue
            if aid in _win_credit_recheck_since:
                continue
            _win_credit_recheck_since[aid] = now
            fresh.append(aid)
    return fresh


def clear_win_credit_recheck(account_ids: list[str]) -> None:
    with _win_credit_recheck_lock:
        for aid in account_ids:
            _win_credit_recheck_since.pop(str(aid).strip(), None)


def is_balance_too_low_for_ws(row: dict[str, Any], cfg: dict[str, Any]) -> bool:
    return account_balance_vnd(row) < min_balance_for_ws(cfg)


def should_disconnect_ws_at_round_start(
    row: dict[str, Any], cfg: dict[str, Any]
) -> bool:
    if is_balance_too_low_for_ws(row, cfg):
        return True
    return exceeds_ws_daily_cap(row, cfg)


def should_disconnect_ws_cap_at_round_start(
    row: dict[str, Any], cfg: dict[str, Any]
) -> bool:
    """Đầu phiên T+0: chỉ ngắt cap — balance chờ delay (thưởng có thể về trễ)."""
    return exceeds_ws_daily_cap(row, cfg)


def is_ws_pool_active_status(row: dict[str, Any], cfg: dict[str, Any]) -> bool:
    """Nick được giữ WS pool — mặc định «Đang Chơi» (game_worker.account_status)."""
    expected = ws_account_status(cfg)
    return str(row.get("status") or "").strip() == expected


def should_disconnect_ws_wrong_status_at_round_start(
    row: dict[str, Any], cfg: dict[str, Any]
) -> bool:
    """Đầu phiên: WS đang mở nhưng DB đã đổi status (Hết Tiền, Đủ ngày, …)."""
    return not is_ws_pool_active_status(row, cfg)


def _disconnect_reason_label(row: dict[str, Any], cfg: dict[str, Any]) -> str:
    min_bal = min_balance_for_ws(cfg)
    daily = daily_bet_today_vnd(row)
    cap = daily_bet_cap_vnd(cfg)
    step = bet_step_vnd(cfg)
    if not is_ws_pool_active_status(row, cfg):
        st = str(row.get("status") or "").strip() or "?"
        return f"status={st} (cần {ws_account_status(cfg)})"
    if is_balance_too_low_for_ws(row, cfg):
        return f"balance<{min_bal:,}"
    if exceeds_ws_daily_cap(row, cfg):
        return f"cược ngày {daily:,.0f}+{step:,}>{cap:,}"
    return "?"


def _pending_bet_skip_ids() -> set[str]:
    """C = chờ KQ (pending_bet) — HOLD status/WS."""
    try:
        from xoso66_auto_bet import pending_bet_account_ids

        return pending_bet_account_ids()
    except Exception:
        return set()


def list_C_pending_kq_ids() -> set[str]:
    """List C: nick còn lệnh cược chờ kết quả phiên."""
    return _pending_bet_skip_ids()


def _accounts_to_disconnect_filtered(
    cfg: dict[str, Any],
    open_account_ids: list[str],
    predicate,
    *,
    log_prefix: str,
) -> list[str]:
    skip_pending = _pending_bet_skip_ids()
    removed: list[str] = []
    for aid in open_account_ids:
        aid = str(aid).strip()
        if not aid or aid in skip_pending:
            continue
        if is_ws_listener(aid, cfg):
            continue
        row = get_account(aid) or {}
        if predicate(row, cfg):
            removed.append(aid)
    if removed and log_prefix:
        details = ", ".join(
            f"{username_for_log(a)} ({_disconnect_reason_label(get_account(a) or {}, cfg)})"
            for a in removed
        )
        print(f"{log_prefix}: {details}", flush=True)
    return removed


def accounts_to_disconnect_cap_at_round_start(
    cfg: dict[str, Any], open_account_ids: list[str]
) -> list[str]:
    """Đầu phiên T+0: chỉ ngắt nick gần đủ cap cược ngày."""
    return _accounts_to_disconnect_filtered(
        cfg,
        open_account_ids,
        should_disconnect_ws_cap_at_round_start,
        log_prefix="[WS-POOL] Phiên mới — ngắt WS (cap)",
    )


def accounts_to_disconnect_wrong_status_at_round_start(
    cfg: dict[str, Any],
    open_account_ids: list[str],
    *,
    log_prefix: str = "[WS-POOL] Phiên mới — ngắt WS (không còn Đang Chơi)",
) -> list[str]:
    """Ngắt WS nick không còn «Đang Chơi» trong DB (đầu phiên / resync định kỳ)."""
    return _accounts_to_disconnect_filtered(
        cfg,
        open_account_ids,
        should_disconnect_ws_wrong_status_at_round_start,
        log_prefix=log_prefix,
    )


def _ws_ids_for_round_status_check(
    open_account_ids: list[str],
) -> list[str]:
    """Task WS + nick đã connect (tránh sót khi task chưa sync)."""
    ids = {str(x).strip() for x in open_account_ids if str(x).strip()}
    ids |= {str(x).strip() for x in get_connected_ws_accounts() if str(x).strip()}
    return sorted(ids)


def accounts_to_disconnect_at_round_start(
    cfg: dict[str, Any], open_account_ids: list[str]
) -> list[str]:
    """Sau KQ / check đủ điều kiện: balance thấp hoặc cap."""
    return _accounts_to_disconnect_filtered(
        cfg,
        open_account_ids,
        should_disconnect_ws_at_round_start,
        log_prefix="[WS-POOL] Phiên mới — ngắt WS",
    )


def filter_ws_target_connectable(
    cfg: dict[str, Any],
    account_ids: list[str],
    *,
    ws_task_ids: list[str] | None = None,
) -> list[str]:
    """
    Mục tiêu WS: status pool/list ưu tiên, đủ tiền (hoặc chờ thưởng / chờ KQ), chưa đủ cap.
    Nick còn lệnh cược pending (chưa KQ) — giữ WS dù DB < min (tránh tắt nhầm giữa phiên).
    """
    tasks = {str(x).strip() for x in (ws_task_ids or []) if str(x).strip()}
    bet_pending = _pending_bet_skip_ids()
    min_bal = min_balance_for_ws(cfg)
    out: list[str] = []
    for aid in account_ids:
        aid = str(aid).strip()
        if not aid:
            continue
        row = get_account(aid) or {}
        if is_ws_listener(aid, cfg):
            out.append(aid)
            continue
        from xoso66_cf import is_account_cf_rate_limited

        if is_account_cf_rate_limited(aid):
            continue
        from xoso66_proxy import is_proxy_dead

        if is_proxy_dead(aid):
            continue
        # Đang chờ KQ: giữ task/connect dù thiếu tiền / gần cap.
        if aid in bet_pending and (aid in tasks or aid in {
            str(x).strip() for x in get_connected_ws_accounts() if str(x).strip()
        }):
            out.append(aid)
            continue
        # Đã có task: chỉ giữ Đang Chơi — không giữ Đủ ngày/Hết Tiền.
        if aid in tasks:
            if not is_ws_pool_active_status(row, cfg):
                continue
            if not row_allowed_for_ws(row):
                continue
        elif not row_eligible_for_ws_fill(row, cfg):
            continue
        if exceeds_ws_daily_cap(row, cfg):
            continue
        # Chỉ chặn đang NẠP — pending-open (= sắp/mở WS) không được chặn spawn
        # (request_ws_open mark pending trước apply_pool → trước đây tự hủy mở).
        if account_deposit_in_flight(aid, cfg) or is_ws_deposit_thread_scheduled(aid):
            if aid not in tasks:
                continue
        bal = account_balance_vnd(row)
        try:
            from xoso66_bet_assign import _auto_bet_cfg, consolidate_skips_underfunded_ws

            strat3_skip = consolidate_skips_underfunded_ws(_auto_bet_cfg(cfg))
        except Exception:
            strat3_skip = False
        if bal < min_bal:
            if strat3_skip:
                continue
            # Chờ thưởng thắng (multi-retry) hoặc còn lệnh chờ KQ.
            if not is_win_credit_recheck_pending(aid) and aid not in bet_pending:
                continue
        out.append(aid)
    return out


def _kept_for_ws_target(
    cfg: dict[str, Any],
    kept: list[str],
    win_credit_wait: list[str],
    *,
    ws_task_ids: list[str] | None = None,
) -> list[str]:
    """
    Nick giữ trong target (đã có / đang WS):
    - Chỉ «Đang Chơi» (không giữ Đủ ngày/Hết Tiền qua row_eligible fill-list).
    - Thiếu tiền: chờ thưởng thắng (win_credit_wait) hoặc còn lệnh cược chờ KQ.
    - Đang nạp (deposit in-flight): không mở WS mới, không chiếm target.
    """
    tasks = {str(x).strip() for x in (ws_task_ids or []) if str(x).strip()}
    wait_set = {str(x).strip() for x in win_credit_wait if str(x).strip()}
    bet_pending = _pending_bet_skip_ids()
    min_bal = min_balance_for_ws(cfg)
    out: list[str] = []
    for aid in kept:
        aid = str(aid).strip()
        if not aid:
            continue
        row = get_account(aid) or {}
        # Đang chờ KQ — giữ task dù status/balance tạm lệch.
        if aid in bet_pending and aid in tasks:
            out.append(aid)
            continue
        # Không dùng row_eligible (gồm list Đủ ngày/Hết Tiền) — chỉ giữ Đang Chơi.
        if not is_ws_pool_active_status(row, cfg):
            continue
        if not row_allowed_for_ws(row):
            continue
        if account_deposit_in_flight(aid, cfg) or is_ws_deposit_thread_scheduled(aid):
            if aid not in tasks:
                continue
        try:
            from xoso66_bet_assign import _auto_bet_cfg, consolidate_skips_underfunded_ws

            strat3_skip = consolidate_skips_underfunded_ws(_auto_bet_cfg(cfg))
        except Exception:
            strat3_skip = False
        bal = account_balance_vnd(row)
        if bal < min_bal:
            if strat3_skip:
                continue
            if aid in bet_pending and aid in tasks:
                out.append(aid)
                continue
            if aid not in wait_set or aid not in tasks:
                continue
        out.append(aid)
    return out


def _ws_live_account_ids(
    ws_task_ids: list[str] | None = None,
) -> set[str]:
    """Task WS + nick đã connect."""
    if ws_task_ids is not None:
        tasks = {str(x).strip() for x in ws_task_ids if str(x).strip()}
    else:
        tasks = {
            str(x).strip() for x in get_ws_task_accounts() if str(x).strip()
        }
    connected = {
        str(x).strip() for x in get_connected_ws_accounts() if str(x).strip()
    }
    return tasks | connected


def _has_active_deposit_order(account_id: str, cfg: dict[str, Any]) -> bool:
    """Lệnh nạp DB còn pending (thật) — không gỡ block / không ép Hết Tiền."""
    aid = str(account_id).strip()
    if not aid:
        return False
    ttl = _deposit_cache_ttl_sec(cfg)
    try:
        from xoso66_deposit_orders_db import has_pending_deposit

        return has_pending_deposit(aid, max_age_sec=ttl)
    except Exception:
        return False


def _is_stale_ws_slot_block(
    aid: str,
    cfg: dict[str, Any],
    *,
    ws_live: set[str] | None = None,
) -> bool:
    """
    pending/cache/luồng nạp chiếm slot nhưng không WS + thiếu tiền + không có lệnh nạp thật.
    """
    aid = str(aid).strip()
    if not aid:
        return False
    live = ws_live if ws_live is not None else _ws_live_account_ids()
    if aid in live:
        return False
    row = get_account(aid) or {}
    if not is_ws_pool_active_status(row, cfg):
        return False
    if not is_balance_too_low_for_ws(row, cfg):
        return False
    blocked = (
        aid in get_pending_ws_slot_ids()
        or is_ws_deposit_thread_scheduled(aid)
        or account_deposit_in_flight(aid, cfg)
    )
    if not blocked:
        return False
    return not _has_active_deposit_order(aid, cfg)


def release_stale_ws_blocks_for_stuck_dang_choi(cfg: dict[str, Any]) -> list[str]:
    """
    «Đang Chơi» thiếu tiền kẹt pending/cache WS — gỡ block để bù slot / ép Hết Tiền / nạp.
    """
    ws_live = _ws_live_account_ids()
    released: list[str] = []
    seen: set[str] = set()
    for row in _pool_rows(cfg):
        aid = str(row.get("id") or "").strip()
        if not aid or aid in seen:
            continue
        seen.add(aid)
        if not _is_stale_ws_slot_block(aid, cfg, ws_live=ws_live):
            continue
        release_ws_blocks_after_deposit([aid])
        released.append(aid)
    if released:
        names = ", ".join(username_for_log(a) for a in released[:12])
        extra = f"… +{len(released) - 12}" if len(released) > 12 else ""
        print(
            f"[WS-POOL] Giải phóng slot WS kẹt (thiếu tiền, không nạp thật): "
            f"{names}{extra}",
            flush=True,
        )
    return released


def _account_eligible_balance_recheck(
    aid: str,
    cfg: dict[str, Any],
    *,
    ws_live: set[str],
    require_in_ws_live: bool | None,
) -> bool:
    """
    require_in_ws_live: True = phải có task/connect; False = không có WS;
    None = không lọc theo ws_live (sau KQ phiên).
    """
    aid = str(aid).strip()
    if not aid:
        return False
    if aid in _pending_bet_skip_ids():
        return False
    if require_in_ws_live is True and aid not in ws_live:
        return False
    if require_in_ws_live is False and aid in ws_live:
        return False
    if is_ws_listener(aid, cfg):
        return False
    row = get_account(aid) or {}
    if not is_ws_pool_active_status(row, cfg):
        return False
    if not is_balance_too_low_for_ws(row, cfg):
        return False
    # Off-WS thiếu tiền: luôn recheck (tránh kẹt Đang Chơi chiếm slot oan).
    if require_in_ws_live is False:
        return True
    # On-WS / sau KQ: chờ nếu nạp thật đang chạy.
    if _has_active_deposit_order(aid, cfg):
        return False
    if aid in get_pending_ws_slot_ids() and aid in ws_live:
        return False
    return True


def _accounts_db_low_for_ws_recheck(
    cfg: dict[str, Any],
    account_ids: list[str],
    *,
    ws_task_ids: list[str] | None = None,
) -> list[str]:
    """
    Nick đang WS thật (task/connect) mà DB < min_balance — recheck sau delay.
    Bỏ qua pending slot / đang nạp (chưa mở WS, chưa «Đang Chơi»).
    """
    ws_live = _ws_live_account_ids(ws_task_ids)
    out: list[str] = []
    for aid in account_ids:
        aid = str(aid).strip()
        if _account_eligible_balance_recheck(
            aid, cfg, ws_live=ws_live, require_in_ws_live=True
        ):
            out.append(aid)
    return out


def _accounts_dang_choi_low_off_ws_recheck(
    cfg: dict[str, Any],
    *,
    ws_task_ids: list[str] | None = None,
) -> list[str]:
    """
    «Đang Chơi» DB < min_balance, không còn task/connect WS — recheck sau delay.
    Tránh kẹt: status Đang Chơi, không WS, không vào list nạp Hết Tiền/Đủ ngày.
    """
    ws_live = _ws_live_account_ids(ws_task_ids)
    out: list[str] = []
    seen: set[str] = set()
    for row in _pool_rows(cfg):
        aid = str(row.get("id") or "").strip()
        if not aid or aid in seen:
            continue
        if _account_eligible_balance_recheck(
            aid, cfg, ws_live=ws_live, require_in_ws_live=False
        ):
            seen.add(aid)
            out.append(aid)
    return out


def _collect_round_balance_recheck_ids(
    cfg: dict[str, Any],
    kept_task_ids: list[str],
    *,
    ws_task_ids: list[str] | None = None,
) -> list[str]:
    """WS live thiếu tiền + «Đang Chơi» thiếu tiền không WS (dedupe)."""
    release_stale_ws_blocks_for_stuck_dang_choi(cfg)
    on_ws = _accounts_db_low_for_ws_recheck(
        cfg, kept_task_ids, ws_task_ids=ws_task_ids
    )
    off_ws = _accounts_dang_choi_low_off_ws_recheck(cfg, ws_task_ids=ws_task_ids)
    return list(dict.fromkeys([*on_ws, *off_ws]))


def _split_recheck_immediate_off_ws(
    cfg: dict[str, Any],
    account_ids: list[str],
    *,
    ws_task_ids: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """
    Off-WS thiếu tiền (không lệnh nạp thật) → recheck ngay (delay 0).
    On-WS / còn nạp → delay bình thường (chờ thưởng WS).
    """
    ws_live = _ws_live_account_ids(ws_task_ids)
    immediate: list[str] = []
    delayed: list[str] = []
    for aid in account_ids:
        aid = str(aid).strip()
        if not aid:
            continue
        if aid not in ws_live and not _has_active_deposit_order(aid, cfg):
            immediate.append(aid)
        else:
            delayed.append(aid)
    return immediate, delayed


def _reserve_round_balance_recheck(
    account_ids: list[str], delay: float
) -> list[str]:
    """Mỗi nick chỉ một timer recheck — tránh spam log / race nhiều thread."""
    now = time.time()
    out: list[str] = []
    with _round_balance_recheck_lock:
        for aid in account_ids:
            aid = str(aid).strip()
            if not aid:
                continue
            last = _round_balance_recheck_pending.get(aid, 0.0)
            if last and (now - last) < max(1.0, delay):
                continue
            _round_balance_recheck_pending[aid] = now
            out.append(aid)
    return out


def _release_round_balance_recheck(account_ids: list[str]) -> None:
    with _round_balance_recheck_lock:
        for aid in account_ids:
            _round_balance_recheck_pending.pop(str(aid).strip(), None)


def _apply_balance_recheck_after_delay(
    cfg: dict[str, Any], account_ids: list[str], *, delay: float
) -> None:
    """
    Sau delay: refresh getBalance → vẫn thiếu tiền (DB) hoặc đủ cap → Hết Tiền + ngắt WS.
    Bỏ qua nick đang multi-retry chờ thưởng thắng (schedule_win_credit_balance_recheck).
    """
    from xoso66_shutdown import stopping

    ids = [str(x).strip() for x in account_ids if str(x).strip()]
    if not ids or stopping():
        _release_round_balance_recheck(ids)
        return

    min_bal = min_balance_for_ws(cfg)
    skip_pending = _pending_bet_skip_ids()
    removed: list[str] = []
    try:
        for aid in ids:
            if stopping():
                break
            if is_win_credit_recheck_pending(aid):
                continue
            if aid in skip_pending:
                continue
            if is_ws_listener(aid, cfg):
                continue
            if _is_stale_ws_slot_block(aid, cfg):
                release_ws_blocks_after_deposit([aid])
            elif aid in get_pending_ws_slot_ids() or (
                account_deposit_in_flight(aid, cfg)
                and _has_active_deposit_order(aid, cfg)
            ):
                continue
            row_before = get_account(aid) or {}
            db_low_before = is_balance_too_low_for_ws(row_before, cfg)
            db_before = account_balance_vnd(row_before)
            _balance_vnd_after_site_refresh(aid)
            if not stopping():
                _, _, err = sync_live_balance_vnd(aid)
                if err and db_low_before:
                    print(
                        f"[WS-POOL] {username_for_log(aid)} refresh balance "
                        f"(sau {delay:.0f}s): {err}",
                        flush=True,
                    )
            row_after = get_account(aid) or {}
            db_after = account_balance_vnd(row_after)
            if not should_disconnect_ws_at_round_start(row_after, cfg):
                if db_low_before and db_after >= min_bal:
                    print(
                        f"[WS-POOL] {username_for_log(aid)} sau {delay:.0f}s "
                        f"đủ tiền (DB {db_after:,.0f} >= {min_bal:,}"
                        f"{f'; trước {db_before:,.0f}' if db_before < min_bal else ''}"
                        f") — giữ WS",
                        flush=True,
                    )
                continue
            removed.append(aid)
    finally:
        _release_round_balance_recheck(ids)

    if not removed or stopping():
        return
    _evict_het_tien_now(
        cfg, removed, log_label="Ép Hết Tiền (recheck balance)"
    )


def _evict_het_tien_now(
    cfg: dict[str, Any],
    account_ids: list[str],
    *,
    log_label: str,
) -> list[str]:
    """Ép Hết Tiền qua cổng Status + Close; resync bù slot nếu thiếu."""
    removed = filter_ws_evict_ids(
        [str(x).strip() for x in account_ids if str(x).strip()], cfg
    )
    if not removed:
        return []
    clear_win_credit_recheck(removed)
    release_ws_blocks_after_deposit(removed)
    names = ", ".join(username_for_log(a) for a in removed[:12])
    extra = f"… +{len(removed) - 12}" if len(removed) > 12 else ""
    print(f"[WS-POOL] {log_label}: {names}{extra}", flush=True)
    _reconcile_begin()
    for aid in removed:
        apply_account_status_intent(aid, STATUS_HET_TIEN, reason="ngắt WS")
    closed = request_ws_close(
        removed,
        reason=log_label,
        flow=f"_evict_het_tien_now/{log_label}",
        cfg=cfg,
    )
    request_ws_evict_and_resync(closed or removed)
    need = ws_slots_need_fill(
        cfg, task_ids=get_ws_task_accounts(), exclude=set(removed)
    )
    if need > 0:
        schedule_fund_deposit_for_ws_shortage(
            cfg, removed, label="ws-pool-after-het-tien"
        )
    return removed


def _refresh_balance_for_recheck(aid: str, *, db_low_before: bool, attempt_label: str) -> float:
    """Refresh site + sync DB; trả balance DB sau refresh."""
    db_before = account_balance_vnd(get_account(aid) or {})
    _balance_vnd_after_site_refresh(aid)
    _, _, err = sync_live_balance_vnd(aid)
    if err and db_low_before:
        print(
            f"[WS-POOL] {username_for_log(aid)} {attempt_label} refresh balance: {err}",
            flush=True,
        )
    row_after = get_account(aid) or {}
    db_after = account_balance_vnd(row_after)
    _ = db_before
    return db_after


def schedule_win_credit_balance_recheck(
    cfg: dict[str, Any], account_ids: list[str]
) -> None:
    """
    Nick thắng, DB < min: giữ WS, refresh theo mốc 10/20/30/40/50s (tối đa 5).
    Lần cuối vẫn thiếu → Hết Tiền.
    """
    delays = win_credit_recheck_delays_sec(cfg)
    if not delays:
        delays = [10.0, 20.0, 30.0, 40.0, 50.0]
    fresh = mark_win_credit_recheck(
        [str(x).strip() for x in account_ids if str(x).strip()]
    )
    if not fresh:
        return
    n = len(delays)

    def _worker() -> None:
        from xoso66_shutdown import sleep_interruptible, stopping

        min_bal = min_balance_for_ws(cfg)
        still = list(fresh)
        t0 = time.time()
        for attempt_i, delay in enumerate(delays):
            if not still or stopping():
                clear_win_credit_recheck(still)
                return
            wait = float(delay) - (time.time() - t0)
            if wait > 0:
                if not sleep_interruptible(wait) or stopping():
                    clear_win_credit_recheck(still)
                    return
            next_still: list[str] = []
            to_evict: list[str] = []
            last = attempt_i >= len(delays) - 1
            label = f"lần {attempt_i + 1}/{n}"
            for aid in still:
                if stopping():
                    clear_win_credit_recheck(still)
                    return
                if not is_win_credit_recheck_pending(aid):
                    continue
                if aid in _pending_bet_skip_ids():
                    next_still.append(aid)
                    continue
                if is_ws_listener(aid, cfg):
                    clear_win_credit_recheck([aid])
                    continue
                row_before = get_account(aid) or {}
                db_low_before = is_balance_too_low_for_ws(row_before, cfg)
                if not db_low_before:
                    clear_win_credit_recheck([aid])
                    continue
                db_after = _refresh_balance_for_recheck(
                    aid, db_low_before=True, attempt_label=label
                )
                if db_after >= min_bal:
                    print(
                        f"[WS-POOL] {username_for_log(aid)} {label}: "
                        f"đủ tiền (DB {db_after:,.0f} >= {min_bal:,}) — giữ WS",
                        flush=True,
                    )
                    clear_win_credit_recheck([aid])
                    continue
                if exceeds_ws_daily_cap(get_account(aid) or {}, cfg):
                    to_evict.append(aid)
                    continue
                if last:
                    print(
                        f"[WS-POOL] {username_for_log(aid)} {label}: "
                        f"vẫn thiếu (DB {db_after:,.0f} < {min_bal:,}) — ép Hết Tiền",
                        flush=True,
                    )
                    to_evict.append(aid)
                else:
                    next_still.append(aid)
            if to_evict:
                _evict_het_tien_now(
                    cfg,
                    to_evict,
                    log_label="Ép Hết Tiền (hết chờ thưởng thắng)",
                )
            still = next_still
        clear_win_credit_recheck(still)

    threading.Thread(
        target=_worker, name="ws-win-credit-recheck", daemon=True
    ).start()


def schedule_round_start_balance_prune(
    cfg: dict[str, Any], recheck_account_ids: list[str]
) -> None:
    """Off-WS / fallback: recheck sau delay ngắn rồi ép Hết Tiền nếu vẫn thiếu."""
    ids = [str(x).strip() for x in recheck_account_ids if str(x).strip()]
    if not ids:
        return
    # Không đụng nick đang multi-retry chờ thưởng thắng.
    ids = [a for a in ids if not is_win_credit_recheck_pending(a)]
    if not ids:
        return
    delay = round_start_balance_check_delay_sec(cfg)
    immediate, delayed = _split_recheck_immediate_off_ws(cfg, ids)
    if immediate:
        imm = _reserve_round_balance_recheck(immediate, 0.0)
        if imm:

            def _run_now() -> None:
                _apply_balance_recheck_after_delay(cfg, imm, delay=0.0)

            threading.Thread(
                target=_run_now, name="ws-round-bal-prune-now", daemon=True
            ).start()
    if not delayed:
        return
    ids = _reserve_round_balance_recheck(delayed, delay)
    if not ids:
        return

    if delay <= 0:

        def _run_delayed_now() -> None:
            _apply_balance_recheck_after_delay(cfg, ids, delay=0.0)

        threading.Thread(
            target=_run_delayed_now, name="ws-round-bal-prune", daemon=True
        ).start()
        return

    def _worker() -> None:
        from xoso66_shutdown import sleep_interruptible, stopping

        if not sleep_interruptible(delay) or stopping():
            _release_round_balance_recheck(ids)
            return
        _apply_balance_recheck_after_delay(cfg, ids, delay=delay)

    threading.Thread(
        target=_worker,
        name="ws-round-bal-prune",
        daemon=True,
    ).start()


def prune_ws_after_settlement(
    cfg: dict[str, Any],
    account_ids: list[str],
    *,
    slots: list[Any] | None = None,
    open_data: dict[str, Any] | None = None,
) -> None:
    """
    Sau KQ (không đóng WS / không đổi status đủ-cap):
    - Thắng + DB < min → giữ WS, multi-retry refresh (win-credit).
    - Đủ cap / thua / thiếu tiền: để Phiên mới (việc2 Đủ ngày, việc1 Hết Tiền, việc5 đóng WS).
    """
    from xoso66_ws_balance import normalize_bet_side, resolve_winning_side

    winning = resolve_winning_side(open_data) if open_data else None
    winner_ids: set[str] = set()
    if slots and winning:
        for s in slots:
            aid = str(getattr(s, "account_id", "") or "").strip()
            if not aid:
                continue
            side = normalize_bet_side(str(getattr(s, "side", "") or ""))
            if side == winning:
                winner_ids.add(aid)
    elif account_ids:
        pass

    ws_live = _ws_live_account_ids()
    win_wait: list[str] = []

    # Bettors + mọi nick WS live (sót nick thiếu tiền không cược phiên này).
    check_ids = list(
        dict.fromkeys(
            [
                *[str(x).strip() for x in account_ids if str(x).strip()],
                *sorted(ws_live),
            ]
        )
    )

    for aid in check_ids:
        if not aid or is_ws_listener(aid, cfg):
            continue
        row = get_account(aid) or {}
        if not is_balance_too_low_for_ws(row, cfg):
            continue
        if aid in winner_ids:
            win_wait.append(aid)
        # Thua / không cược / thiếu tiền / đủ cap — không ép; Phiên mới xử lý.

    if win_wait:
        schedule_win_credit_balance_recheck(cfg, win_wait)


def connected_shortage_actions(
    cfg: dict[str, Any],
    *,
    extra_exclude: set[str] | None = None,
    task_ids: list[str] | None = None,
    log_context: str = "Resync — bù slot",
) -> tuple[list[str], list[str]]:
    """Thiếu bao nhiêu slot thì bù bấy nhiêu nick (đã tính pending/cache/task)."""
    tasks = task_ids if task_ids is not None else get_ws_pool_scope_ids()
    need = ws_slots_need_fill(cfg, task_ids=tasks)
    if need <= 0:
        return [], []
    _log_ws_slot_need(cfg, task_ids=tasks, need=need, log_context=log_context)
    ex = set(extra_exclude or set())
    return plan_ws_slot_fill(
        cfg,
        need,
        exclude=ex,
        prefer_dang_choi=True,
        log_context="",
    )


def select_ws_account_ids(cfg: dict[str, Any]) -> list[str]:
    """Nick đủ tiền để mở WS (không gồm acc cần nạp trước)."""
    connect, _ = pick_ws_target_account_ids(cfg)
    return connect


def _deposit_order(
    account_ids: list[str],
    *,
    min_bal: int,
    cap: int,
    priority_ids: list[str] | None,
    cfg: dict[str, Any],
) -> list[str]:
    """Thứ tự nạp: priority trước, còn lại theo game_worker.ws_fill_priority."""
    need: list[tuple[str, float, float]] = []
    for aid in account_ids:
        row = get_account(aid) or {}
        bal = account_balance_vnd(row)
        if bal >= min_bal:
            continue
        if daily_bet_today_vnd(row) >= cap:
            print(
                f"[WS-POOL] Bỏ nạp {username_for_log(aid)}: đã đủ cap cược ngày "
                f"({daily_bet_today_vnd(row):,.0f} / {cap:,})",
                flush=True,
            )
            continue
        need.append((aid, daily_bet_today_vnd(row), bal))

    if not need:
        return []

    pri = [str(x) for x in (priority_ids or []) if str(x).strip()]
    pri_set = set(pri)
    first = [t for t in need if t[0] in pri_set]
    first.sort(key=lambda x: pri.index(x[0]) if x[0] in pri else 999)
    rest = [t for t in need if t[0] not in pri_set]
    fill_mode = ws_fill_priority_mode(cfg)
    if fill_mode == 0:
        rest.sort(key=lambda x: (x[2], -x[1], x[0]))
    elif fill_mode == 2:
        # need tuple: (aid, daily, bal) → cược ngày↓ rồi số dư↓
        rest.sort(key=lambda x: (-x[1], -x[2], x[0]))
    else:
        rest.sort(key=lambda x: (x[1], -x[2], x[0]))
    return [t[0] for t in first + rest]


def deposit_wait_confirm(cfg: dict[str, Any]) -> bool:
    """Chờ poll Hoàn tất (game_worker.deposit_wait_confirm; nạp WS luôn poll)."""
    gw = game_worker_cfg(cfg)
    if "deposit_wait_confirm" in gw:
        return bool(gw.get("deposit_wait_confirm"))
    ad = cfg.get("auto_deposit")
    if isinstance(ad, dict) and "wait_confirm" in ad:
        return bool(ad.get("wait_confirm"))
    return False


def _fail_deposit_order_and_release(
    aid: str, order_id: int, *, error: str, log_prefix: str = "[WS-POOL]"
) -> None:
    """Poll hết lần / thất bại — ghi Thất Bại + xóa cache + bỏ pending slot."""
    from xoso66_deposit_tracking import deposit_order_confirmed

    oid = int(order_id)
    from xoso66_deposit_orders_db import get_deposit_order, update_deposit_order

    if not deposit_order_confirmed(oid):
        row = get_deposit_order(oid) or {}
        st = str(row.get("status") or "").strip()
        if st not in ("Thành Công", "Huỷ", "Hủy", "Thất Bại"):
            update_deposit_order(oid, status="Thất Bại", error_message=str(error or ""))
    u = username_for_log(aid)
    print(
        f"{log_prefix} Đơn #{oid} [{u}] Thất Bại — {error}",
        flush=True,
    )
    release_ws_blocks_after_deposit([aid])


def _finalize_deposit_order_confirmed(
    aid: str, order_id: int, poll_rep: dict[str, Any]
) -> None:
    """Ghi DB đơn nạp Thành Công + bỏ cache (giống [DEPOSIT-POLL])."""
    item = poll_rep.get("item") if isinstance(poll_rep.get("item"), dict) else {}
    serial = str(poll_rep.get("serial_no") or item.get("serial_no") or "")
    from xoso66_deposit_orders_db import finalize_deposit_success

    finalize_deposit_success(
        int(order_id),
        serial_no=serial,
        site_status=1,
        site_status_formatted=str(item.get("status_formatted") or "Hoàn tất"),
        game_item=item,
    )
    try:
        from xoso66_auto_deposit import remove_from_deposit_cache

        remove_from_deposit_cache(aid)
    except Exception:
        pass
    release_ws_blocks_after_deposit([aid])


def _wait_deposit_confirmed(cfg: dict[str, Any], aid: str, rep: dict[str, Any]) -> bool:
    order_id = rep.get("order_id")
    if not order_id:
        return False
    oid = int(order_id)
    from xoso66_deposit_orders_db import get_deposit_order
    from xoso66_deposit_tracking import (
        deposit_order_confirmed,
        end_deposit_poll,
        poll_deposit_until_confirmed,
        try_begin_deposit_poll,
    )
    from xoso66_session import ensure_session

    if deposit_order_confirmed(oid):
        print(
            f"[WS-POOL] Nạp Hoàn tất #{oid} {username_for_log(aid)} — đơn đã Thành Công (poll khác)",
            flush=True,
        )
        return True
    if not try_begin_deposit_poll(oid):
        if deposit_order_confirmed(oid):
            print(
                f"[WS-POOL] Nạp Hoàn tất #{oid} {username_for_log(aid)} — DEPOSIT-POLL đang/đã xử lý",
                flush=True,
            )
            return True
        print(
            f"[WS-POOL] Poll nạp #{oid} {username_for_log(aid)} — bỏ (đơn đang poll ở handler)",
            flush=True,
        )
        return False

    row = get_deposit_order(oid) or {}
    serial_no = str(row.get("serial_no") or rep.get("serial_no") or "").strip()
    ad = cfg.get("auto_deposit") if isinstance(cfg.get("auto_deposit"), dict) else {}
    interval = float(ad.get("poll_interval_sec") or 10)
    max_attempts = int(ad.get("poll_max_attempts") or 100)
    list_limit = int(ad.get("deposit_list_limit") or 10)

    try:
        session = ensure_session(aid, force_login=False)
    except Exception as e:
        print(f"[WS-POOL] Nạp poll {username_for_log(aid)}: không có session — {e}", flush=True)
        return False

    try:
        poll_rep = poll_deposit_until_confirmed(
            session,
            account_id=aid,
            serial_no=serial_no,
            poll_interval_sec=interval,
            max_attempts=max_attempts,
            list_limit=list_limit,
            order_id=oid,
        )
        if poll_rep.get("cancelled"):
            err = str(poll_rep.get("error") or "đã hủy")
            print(
                f"[WS-POOL] Poll nạp #{oid} {username_for_log(aid)} — {err}",
                flush=True,
            )
            _fail_deposit_order_and_release(aid, oid, error=err)
            return False
        if poll_rep.get("success"):
            via = str(poll_rep.get("via") or "")
            if via == "order_already_thanh_cong":
                print(
                    f"[WS-POOL] Nạp Hoàn tất #{oid} {username_for_log(aid)} — đã Thành Công (poll khác)",
                    flush=True,
                )
                return True
            print(f"[WS-POOL] Nạp Hoàn tất #{oid} {username_for_log(aid)}", flush=True)
            if not deposit_order_confirmed(oid):
                _finalize_deposit_order_confirmed(aid, oid, poll_rep)
            return True
        err = str(poll_rep.get("error") or "timeout")
        print(
            f"[WS-POOL] Nạp chưa Hoàn tất #{oid} {username_for_log(aid)}: {err}",
            flush=True,
        )
        _fail_deposit_order_and_release(aid, oid, error=err)
        return False
    finally:
        end_deposit_poll(oid)


def open_ws_after_deposit_confirmed(
    account_ids: list[str], cfg: dict[str, Any] | None = None
) -> None:
    """
    Sau nạp Hoàn tất: set «Đang Chơi» + xếp mở WS (pending → worker spawn).
    Phiên mới việc4 thấy đã ∈ A thì không gửi mở lại.
    """
    ids = [str(x).strip() for x in account_ids if str(x).strip()]
    if not ids:
        return
    if cfg is None:
        from xoso66_config_util import load_config

        cfg = load_config()
    release_ws_blocks_after_deposit(ids)
    done: list[str] = []
    for aid in ids:
        if apply_account_status_intent(aid, STATUS_DANG_CHOI, reason="nạp xong"):
            done.append(aid)
        elif str((get_account(aid) or {}).get("status") or "").strip() == STATUS_DANG_CHOI:
            done.append(aid)
    if not done:
        return
    names = ", ".join(username_for_log(a) for a in done[:12])
    extra = f" … +{len(done) - 12}" if len(done) > 12 else ""
    # Xếp worker trước (chưa pending), rồi mark pending — tránh schedule bỏ qua vì đã ∈ A.
    try:
        from xoso66_minigame_ws_worker import schedule_ws_connect_after_deposit

        schedule_ws_connect_after_deposit(done)
    except Exception as e:
        print(f"[WS-POOL] Nạp xong — không xếp mở WS: {e}", flush=True)
    opened = request_ws_open(
        done,
        reason="nạp xong",
        cfg=cfg,
        ensure_dang_choi=False,
        quiet=True,
    )
    print(
        f"[WS-POOL] Nạp Hoàn tất — Đang Chơi + mở WS: {names}{extra}"
        + (f" (xếp {len(opened)})" if opened else ""),
        flush=True,
    )


def fund_accounts_below_minimum(
    cfg: dict[str, Any],
    account_ids: list[str],
    *,
    priority_ids: list[str] | None = None,
    wait_confirm: bool | None = None,
    open_ws_on_confirm: bool = False,
) -> dict[str, Any]:
    """
    Nạp acc balance < min_balance_vnd (nếu auto_deposit bật).
    Nhiều nick trong cùng batch → tạo lệnh + poll Hoàn tất song song
    (không chờ nick trước xong mới nạp nick sau).
    """
    ad = cfg.get("auto_deposit")
    if not (isinstance(ad, dict) and ad.get("enabled")):
        min_bal = min_balance_for_ws(cfg)
        under = [
            aid
            for aid in account_ids
            if account_balance_vnd(get_account(aid) or {}) < min_bal
        ]
        if under:
            print(
                f"[WS-POOL] Cần nạp {len(under)} acc nhưng auto_deposit.enabled=false",
                flush=True,
            )
        return {"ok": False, "skipped": True, "underfunded": under}

    from xoso66_auto_deposit import (
        _wait_deposit_slot,
        can_create_deposit_order,
        default_amount,
        perform_deposit,
    )

    min_bal = min_balance_for_ws(cfg)
    cap = int(daily_bet_ws_limit_vnd(cfg))
    order = _deposit_order(
        account_ids,
        min_bal=min_bal,
        cap=cap,
        priority_ids=priority_ids,
        cfg=cfg,
    )

    ok_ids: list[str] = []
    fail: list[dict[str, str]] = []
    amt = default_amount()
    should_wait = (
        bool(wait_confirm)
        if wait_confirm is not None
        else deposit_wait_confirm(cfg)
    )
    if open_ws_on_confirm:
        should_wait = True

    from xoso66_shutdown import stopping

    result_lock = threading.Lock()

    def _fund_one(aid: str) -> None:
        if stopping():
            return
        try:
            from xoso66_proxy import is_proxy_dead

            if is_proxy_dead(aid):
                print(
                    f"[WS-POOL] Bỏ nạp {username_for_log(aid)}: proxy chết / Lỗi proxy",
                    flush=True,
                )
                return
        except Exception:
            pass
        if not can_create_deposit_order(aid):
            from xoso66_auto_deposit import deposit_order_block_reason

            reason = deposit_order_block_reason(aid) or "không tạo được đơn"
            print(
                f"[WS-POOL] Bỏ nạp {username_for_log(aid)}: {reason}",
                flush=True,
            )
            return
        if not account_needs_deposit_live(aid, min_bal):
            try:
                from xoso66_proxy import is_proxy_dead

                if is_proxy_dead(aid):
                    return
            except Exception:
                pass
            restore_funded_ws_pool_accounts(cfg, [aid])
            return
        _wait_deposit_slot()
        rep = perform_deposit(aid, amt, verbose=True)
        if rep.get("ok"):
            pending = str(rep.get("status") or rep.get("message") or "").upper()
            is_pending = "PENDING" in pending or "CHỜ" in pending
            confirmed = not is_pending
            if is_pending:
                try:
                    from xoso66_auto_deposit import mark_deposit_cache

                    mark_deposit_cache(aid)
                except Exception:
                    pass
            if is_pending and should_wait:
                confirmed = _wait_deposit_confirmed(cfg, aid, rep)
            elif is_pending and not should_wait:
                confirmed = False
            if confirmed:
                with result_lock:
                    ok_ids.append(aid)
                clear_pending_ws_slot(aid)
                try:
                    from xoso66_session import refresh_account_balance_to_db

                    bal_rep = refresh_account_balance_to_db(aid, force_relogin=False)
                    if bal_rep.get("ok"):
                        print(
                            f"[WS-POOL] {username_for_log(aid)} balance sau nạp: "
                            f"{float(bal_rep.get('balance') or 0):,.0f}",
                            flush=True,
                        )
                except Exception as e:
                    print(
                        f"[WS-POOL] {username_for_log(aid)} refresh balance: {e}",
                        flush=True,
                    )
                if open_ws_on_confirm:
                    open_ws_after_deposit_confirmed([aid], cfg)
            else:
                with result_lock:
                    fail.append(
                        {
                            "id": aid,
                            "error": "pending_not_confirmed"
                            if is_pending
                            else "deposit_incomplete",
                        }
                    )
            return
        err = str(rep.get("error") or "deposit_fail")
        proxy_hit = bool(rep.get("proxy_error"))
        try:
            from xoso66_proxy import maybe_report_proxy_dead_from_message

            if maybe_report_proxy_dead_from_message(aid, err, source="NẠP"):
                proxy_hit = True
        except Exception:
            pass
        with result_lock:
            if proxy_hit:
                print(
                    f"[WS-POOL] Nạp FAIL {username_for_log(aid)}: lỗi proxy — {err}",
                    flush=True,
                )
                fail.append({"id": aid, "error": "proxy_dead", "detail": err})
            else:
                print(f"[WS-POOL] Nạp FAIL {username_for_log(aid)}: {err}", flush=True)
                fail.append({"id": aid, "error": err})

    try:
        if len(order) <= 1:
            for aid in order:
                if stopping():
                    print("[WS-POOL] Dừng nạp (Ctrl+C)", flush=True)
                    break
                _fund_one(aid)
        else:
            workers = min(8, len(order))
            print(
                f"[WS-POOL] Nạp song song {len(order)} nick ({workers} luồng)",
                flush=True,
            )
            from concurrent.futures import ThreadPoolExecutor, as_completed

            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = [ex.submit(_fund_one, aid) for aid in order]
                for fut in as_completed(futs):
                    if stopping():
                        break
                    try:
                        fut.result()
                    except KeyboardInterrupt:
                        raise
                    except Exception as e:
                        print(f"[WS-POOL] Nạp song song lỗi: {e}", flush=True)
    except KeyboardInterrupt:
        print("[WS-POOL] Hủy nạp (Ctrl+C)", flush=True)
        raise

    if fail:
        for f in fail:
            if f.get("error") in (
                "cache_or_pending",
                "pending_not_confirmed",
                "proxy_dead",
            ):
                continue
            print(
                f"[WS-POOL]   → {username_for_log(f.get('id') or '')}: {f.get('error')}",
                flush=True,
            )

    still_under = [
        aid
        for aid in account_ids
        if account_balance_vnd(get_account(aid) or {}) < min_bal
    ]
    return {
        "ok": not still_under,
        "deposited": ok_ids,
        "failed": fail,
        "still_underfunded": still_under,
    }


def prioritize_fund_ws_user(account_id: str, cfg: dict[str, Any] | None = None) -> None:
    """Nạp ưu tiên một acc (poll Hoàn tất → Đang Chơi + mở WS)."""
    from xoso66_config_util import load_config

    cfg = cfg or load_config()
    aid = str(account_id).strip()
    if not aid:
        return
    fund_accounts_below_minimum(
        cfg,
        [aid],
        priority_ids=[aid],
        wait_confirm=True,
        open_ws_on_confirm=True,
    )


def resolve_ws_account_ids(cfg: dict[str, Any], *, force: bool = False) -> list[str]:
    """Chọn N acc WS (cache) — dùng chung startup + prepare_ws_pool."""
    global _pool_cache
    with _pool_cache_lock:
        if _pool_cache and not force:
            return list(_pool_cache)
        ids = select_ws_account_ids(cfg)
        _pool_cache = list(ids)
        return list(ids)


def prime_ws_pool_selection(cfg: dict[str, Any]) -> list[str]:
    """Cache danh sách «Đang Chơi» (không chọn N nick — WS = hết status)."""
    global _selection_logged
    account_ids = resolve_ws_account_ids(cfg)
    if not _selection_logged:
        from xoso66_config_util import startup_quiet

        if not startup_quiet(cfg):
            min_target = ws_account_count(cfg)
            min_bal = min_balance_for_ws(cfg)
            print(
                f"[WS-POOL] Khởi động: tối đa {min_target} WS — "
                f"đủ tiền mở ngay (>= {min_bal:,}), thiếu tiền nạp trước rồi mở WS",
                flush=True,
            )
        _selection_logged = True
    return account_ids


def request_ws_evict_and_resync(account_ids: list[str]) -> None:
    """Ngắt WS acc sắp đủ cap ngày; resync thêm nick khác (gọi từ luồng gán cược)."""
    from xoso66_config_util import load_config
    from xoso66_minigame_ws_worker import schedule_ws_evict_and_resync

    cfg = load_config()
    aids = filter_ws_evict_ids(
        [str(x).strip() for x in account_ids if str(x).strip()], cfg
    )
    if not aids:
        return
    schedule_ws_evict_and_resync(aids)


def prepare_ws_pool(cfg: dict[str, Any]) -> list[str]:
    """
    Khởi động: chỉ mở WS nick đã >= min_balance; nick thiếu tiền → nạp trước, mở WS sau Hoàn tất.
    Tránh mở WS → ngắt (balance thấp) → nạp → mở lại.
    """
    global _pool_cache
    from xoso66_config_util import main_progress

    sync_exhausted_dang_choi_to_du_ngay(cfg)
    min_target = ws_account_count(cfg)
    min_bal = min_balance_for_ws(cfg)

    connect_ids, deposit_ids = _plan_initial_ws_connect_deposit(cfg)

    with _pool_cache_lock:
        _pool_cache = list(connect_ids)

    if connect_ids:
        names_c = ", ".join(username_for_log(a) for a in connect_ids[:8])
        extra_c = f"… +{len(connect_ids) - 8}" if len(connect_ids) > 8 else ""
        main_progress(
            f"[WS-POOL] Khởi động — mở WS {len(connect_ids)} nick "
            f"({names_c}{extra_c}) | đủ tiền >= {min_bal:,}"
        )
    if deposit_ids:
        names_d = ", ".join(username_for_log(a) for a in deposit_ids[:8])
        extra_d = f"… +{len(deposit_ids) - 8}" if len(deposit_ids) > 8 else ""
        main_progress(
            f"[WS-POOL] Khởi động — nạp {len(deposit_ids)} nick trước "
            f"({names_d}{extra_d}) — Hoàn tất → Đang Chơi (WS ở resync phiên)"
        )
    if not connect_ids and not deposit_ids:
        main_progress(
            f"[WS-POOL] Khởi động — không có nick WS (tối đa {min_target} slot)"
        )

    if not is_pool_startup_done() and connect_ids:
        ensure_pool_startup_before_deposit(cfg, connect_ids)

    if deposit_ids:
        from xoso66_shutdown import stopping

        if not stopping():
            schedule_fund_deposit_for_ws_shortage(
                cfg, deposit_ids, label="ws-pool-startup-deposit"
            )

    return connect_ids


def on_round_start_ws_pool(
    game_id: int,
    issue: str,
    next_info: dict[str, Any],
    *,
    reporter: str = "",
) -> None:
    """
    Phiên mới = BẮT ĐẦU phiên game đang theo dõi (hũ cao nhất).

    Game khác: im lặng return (không log, không làm gì).
    5 việc chạy song song / độc lập với luồng cược (không chờ placeOrder).
    """
    _ = next_info, reporter
    from xoso66_shutdown import stopping

    if stopping():
        return
    from xoso66_config_util import load_config

    cfg = load_config()
    if not cfg.get("game_worker_enabled"):
        return
    if not ws_round_sync_enabled():
        return
    # Chỉ game đang theo dõi — game khác bỏ qua im lặng.
    if not round_start_triggers_ws_pool_resync(game_id, cfg):
        return

    gw = cfg.get("game_worker") if isinstance(cfg.get("game_worker"), dict) else {}
    try:
        delay = max(0.0, float(gw.get("ws_pool_resync_delay_after_round_sec") or 8))
    except (TypeError, ValueError):
        delay = 8.0

    def _schedule() -> None:
        if stopping():
            return
        try:
            from xoso66_minigame_ws_worker import schedule_ws_pool_round_check

            schedule_ws_pool_round_check(
                game_id=int(game_id), issue=str(issue or "").strip()
            )
        except Exception as e:
            print(f"[WS-POOL] Phiên mới — không gọi được resync WS: {e}", flush=True)

    import threading

    if delay <= 0:
        threading.Thread(
            target=_schedule, name="ws-pool-round-resync", daemon=True
        ).start()
        return

    threading.Timer(delay, _schedule).start()


def register_ws_pool_round_handler() -> None:
    from xoso66_minigame_ws import register_round_start_handler

    register_round_start_handler(on_round_start_ws_pool)


def start_ws_balance_monitor(cfg: dict[str, Any]) -> threading.Thread | None:
    """Đã tắt — không nạp định kỳ nền (chỉ khởi động + đầu phiên)."""
    return None
