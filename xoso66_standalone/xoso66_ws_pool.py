# -*- coding: utf-8 -*-
"""
Chọn account mở WS mini-game + nạp khi thiếu acc đủ điều kiện.

Ưu tiên danh sách (2 đoạn nối tiếp):
  - balance >= bet_step_vnd: sắp balance cao → thấp (bỏ qua cược ngày).
  - balance < bet_step_vnd: sắp tổng cược ngày cao → thấp (bỏ qua balance).

Đầu phiên / resync (giống LC79 — không roster RAM):
  1) Ngay lập tức: ngắt WS nếu gần đủ cap. Nick DB < min: giữ WS; sau delay refresh site
     → đủ tiền thì giữ; vẫn thiếu thì ngắt WS + Hết Tiền (bù/nạp slot).
  2) Bù slot: tối đa (ws_account_count − slot đang dùng); đếm task + connect + cache nạp + pending.
  3) Không mở hết «Đang Chơi» — chỉ bù đúng need nick.
  Sau KQ: chỉ ngắt WS ngay nếu đủ cap ngày; balance thấp (DB) → recheck sau delay + refresh site.

Ngưỡng tiền WS = bet_step_vnd; cap cược ngày WS = daily_bet_cap_vnd (897k…).
side_total_vnd chỉ dùng chia cược mỗi phiên Tài/Xỉu, không dùng «Đủ ngày».
List ưu tiên (API / nạp): chỉ DB status Hết Tiền hoặc Đủ ngày (+ proxy).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from xoso66_accounts_db import (
    STATUS_DANG_CHOI,
    STATUS_DU_NGAY,
    STATUS_HET_TIEN,
    daily_bet_today_vnd as _daily_bet_from_row,
    get_account,
    list_accounts,
    list_accounts_by_status,
    set_account_status,
    username_for_log,
    usernames_for_log,
)

_ws_task_ids_provider: Callable[[], list[str]] | None = None
_connected_ws_ids: set[str] = set()
_connected_ws_lock = threading.Lock()
_pool_cache: list[str] = []
_pool_cache_lock = threading.Lock()
_selection_logged = False
_pool_startup_done = False
_pool_startup_lock = threading.Lock()
_ws_round_sync_enabled = False
_ws_round_sync_lock = threading.Lock()
_pending_slot_ids: set[str] = set()
_pending_slot_lock = threading.Lock()
_ws_listener_id: str | None = None
_ws_listener_lock = threading.Lock()


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


def count_ws_slots_in_use(
    cfg: dict[str, Any],
    *,
    task_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Đếm slot WS thật (task/connect/pending). Cache nạp chỉ báo thêm, không chặn mở WS."""
    tasks = {
        str(x).strip()
        for x in (task_ids if task_ids is not None else get_ws_task_accounts())
        if str(x).strip()
    }
    connected = {str(x).strip() for x in get_connected_ws_accounts() if str(x).strip()}
    pending = get_pending_ws_slot_ids()
    caching = deposit_cache_account_ids(cfg)
    ws_live = tasks | connected | pending
    cache_only = caching - ws_live
    return {
        "task_n": len(tasks),
        "connected_n": len(connected),
        "deposit_cache_n": len(caching),
        "deposit_cache_extra_n": len(cache_only),
        "pending_n": len(pending),
        "ws_live_n": len(ws_live),
        "in_use_n": len(ws_live),
    }


def ws_slots_need_fill(
    cfg: dict[str, Any], *, task_ids: list[str] | None = None
) -> int:
    """Số slot WS còn thiếu tới ws_account_count (không tính cache nạp)."""
    target = ws_account_count(cfg)
    info = count_ws_slots_in_use(cfg, task_ids=task_ids)
    return max(0, target - int(info["in_use_n"]))


def _log_ws_slot_need(
    cfg: dict[str, Any],
    *,
    task_ids: list[str] | None,
    need: int,
    log_context: str,
) -> None:
    if not log_context:
        return
    info = count_ws_slots_in_use(cfg, task_ids=task_ids)
    target = ws_account_count(cfg)
    print(
        f"[WS-POOL] {log_context}: task={info['task_n']} connect={info['connected_n']} "
        f"cache={info['deposit_cache_n']} pending={info['pending_n']} "
        f"→ bù {need}/{target}",
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
    forced = str(gw.get("ws_listener_account_id") or "").strip()
    with _ws_listener_lock:
        if forced:
            row = get_account(forced) or {}
            if str(row.get("proxy") or "").strip():
                _ws_listener_id = forced
                return forced
        if _ws_listener_id:
            row = get_account(_ws_listener_id) or {}
            if str(row.get("proxy") or "").strip():
                return _ws_listener_id
        for row in list_accounts():
            aid = str(row.get("id") or "").strip()
            if not aid or not str(row.get("proxy") or "").strip():
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
        forced = str(game_worker_cfg(cfg).get("ws_listener_account_id") or "").strip()
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
    """Số nick WS spawn mỗi lô (tránh 56 connect + refresh cùng lúc)."""
    gw = game_worker_cfg(cfg)
    return max(1, int(gw.get("ws_connect_batch_size") or 8))


def ws_connect_batch_delay_sec(cfg: dict[str, Any]) -> float:
    gw = game_worker_cfg(cfg)
    return max(0.0, float(gw.get("ws_connect_batch_delay_sec") or 0.35))


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
    """Cập nhật status DB khi đổi pool WS."""
    for aid in filter_ws_evict_ids(leaving or [], cfg):
        aid = str(aid).strip()
        if not aid:
            continue
        row = get_account(aid) or {}
        off = status_when_leaving_ws(row, cfg)
        if off:
            set_account_status(aid, off, reason="ngắt WS")

    for aid in joining or []:
        aid = str(aid).strip()
        if not aid:
            continue
        row = get_account(aid) or {}
        if str(row.get("status") or "").strip() == STATUS_DANG_CHOI:
            continue
        set_account_status(aid, STATUS_DANG_CHOI, reason="mở WS")


def mark_daily_cap_status(account_ids: list[str], cfg: dict[str, Any]) -> None:
    """Ép Đủ ngày (trước khi ngắt WS vì cap). set_account_status → hẹn auto nhận thưởng."""
    _ = cfg
    for aid in account_ids:
        aid = str(aid).strip()
        if not aid:
            continue
        set_account_status(aid, STATUS_DU_NGAY, reason="đủ cap cược ngày")


# Chỉ các status này được đưa vào list ưu tiên mở WS / nạp (bước 4, API).
WS_OPEN_LIST_STATUSES = frozenset({STATUS_HET_TIEN, STATUS_DU_NGAY})


def _pool_rows(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Pool «Đang Chơi» + proxy — bước 3 đầu phiên (mở WS acc đang chơi)."""
    status = ws_account_status(cfg)
    pool = list_accounts_by_status(status)
    return [a for a in pool if str(a.get("proxy") or "").strip()]


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
            if not str(row.get("proxy") or "").strip():
                continue
            seen.add(aid)
            out.append(row)
    return out


def row_allowed_for_ws_open_list(row: dict[str, Any]) -> bool:
    st = str(row.get("status") or "").strip()
    return st in WS_OPEN_LIST_STATUSES and bool(str(row.get("proxy") or "").strip())


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


def ws_target_occupied_account_ids(cfg: dict[str, Any]) -> set[str]:
    """WS đã connect ∪ cache nạp (không đếm trùng một nick)."""
    connected = {str(x).strip() for x in get_connected_ws_accounts() if str(x).strip()}
    return connected | deposit_cache_account_ids(cfg)


def ws_target_occupied_counts(cfg: dict[str, Any]) -> dict[str, int]:
    info = count_ws_slots_in_use(cfg)
    return {
        "task_n": int(info["task_n"]),
        "connected_n": int(info["connected_n"]),
        "deposit_cache_n": int(info["deposit_cache_n"]),
        "deposit_cache_extra_n": int(info.get("deposit_cache_extra_n", 0)),
        "pending_n": int(info["pending_n"]),
        "occupied_n": int(info["in_use_n"]),
    }


def account_deposit_in_flight(account_id: str, cfg: dict[str, Any]) -> bool:
    """
    Đang nạp (LC79: deposit_pending_cache + lệnh DB chưa terminal).
    Dùng khi đếm slot ws_account_count — tránh nạp thêm khi đã đủ 12 «chỗ».
    """
    aid = str(account_id).strip()
    if not aid:
        return False
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
        if account_deposit_in_flight(aid, cfg):
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
    return sort_rows_ws_priority(rows, cfg)


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
    return sort_rows_ws_priority(rows, cfg)


def ranked_deposit_candidates(
    cfg: dict[str, Any], *, exclude: set[str] | None = None
) -> list[dict[str, Any]]:
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
    under.sort(key=_sort_key_daily_bet_only)
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
            return ids

    pool = _pool_rows(cfg)
    if not pool:
        if allow_empty:
            return []
        raise RuntimeError(f"Không có account status='{status}' (có proxy).")
    return [str(r["id"]) for r in sort_rows_ws_priority(pool, cfg)]


def _row_fill_candidate(
    row: dict[str, Any], cfg: dict[str, Any], *, exclude: set[str]
) -> tuple[str, str] | None:
    """Trả (aid, 'connect'|'deposit') hoặc None."""
    aid = str(row.get("id") or "").strip()
    if not aid or aid in exclude:
        return None
    if exceeds_ws_side_daily_cap(row, cfg):
        return None
    if account_deposit_in_flight(aid, cfg):
        return None
    min_bal = min_balance_for_ws(cfg)
    if account_balance_vnd(row) >= min_bal:
        return aid, "connect"
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


def _log_ws_underfunded_to_deposit(cfg: dict[str, Any], aids: list[str], *, tag: str) -> None:
    if not aids:
        return
    min_bal = min_balance_for_ws(cfg)
    names = ", ".join(username_for_log(a) for a in aids[:8])
    extra = f"… +{len(aids) - 8}" if len(aids) > 8 else ""
    delay = round_start_balance_check_delay_sec(cfg)
    print(
        f"[WS-POOL] {tag} — {len(aids)} nick DB < {min_bal:,}, "
        f"recheck sau {delay:.0f}s (chưa ngắt WS): {names}{extra}",
        flush=True,
    )


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
        for row in sort_rows_ws_priority(_pool_rows(cfg), cfg):
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


def pick_ws_target_account_ids(cfg: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Mở WS chỉ nick đủ tiền; nick thiếu tiền → deposit_ids (nạp trước, mở WS sau)."""
    min_target = ws_account_count(cfg)
    connect, deposit = _classify_rows_connect_deposit(
        sort_rows_ws_priority(_pool_rows(cfg), cfg), cfg
    )
    need = max(0, min_target - len(connect))
    if need > 0:
        fc, fd = plan_ws_slot_fill(
            cfg, need, exclude=set(connect) | set(deposit), prefer_dang_choi=False
        )
        connect = (connect + fc)[:min_target]
        deposit = _merge_unique_account_ids(deposit, fd)
    return connect[:min_target], deposit


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
) -> WsSyncPlan | None:
    """Kế hoạch sync WS; None nếu không cần thay đổi."""
    min_target = ws_account_count(cfg)
    current = [str(x).strip() for x in current_ids if str(x).strip()]

    if round_start:
        prune_removed = filter_ws_evict_ids(
            accounts_to_disconnect_cap_at_round_start(cfg, current), cfg
        )
        for aid in prune_removed:
            clear_pending_ws_slot(aid)
        kept = [a for a in current if a not in prune_removed]
        recheck_low = _accounts_db_low_for_ws_recheck(cfg, kept)
        if recheck_low:
            _log_ws_underfunded_to_deposit(
                cfg, recheck_low, tag="Phiên mới (recheck sau delay)"
            )
        schedule_round_start_balance_prune(cfg, recheck_low)
        kept_target = _kept_for_ws_target(
            cfg, kept, recheck_low, ws_task_ids=ws_task_ids
        )
        task_keys = [
            str(x).strip() for x in (ws_task_ids or []) if str(x).strip()
        ]
        task_set = set(task_keys)
        need = max(0, min_target - len(task_set))
        _log_ws_slot_need(cfg, task_ids=task_keys, need=need, log_context="Phiên mới")
        exclude_fill = task_set | set(prune_removed)
        fill_connect, fill_deposit = plan_ws_slot_fill(
            cfg,
            need,
            exclude=exclude_fill,
            prefer_dang_choi=True,
            log_context="Phiên mới" if need else "",
        )
        if need > 0 and not fill_connect and not fill_deposit:
            fc, fd = plan_priority_fill_ids(
                cfg,
                need,
                exclude=exclude_fill,
                log_context="Phiên mới (list ưu tiên)",
            )
            fill_connect, fill_deposit = fc, fd
        deposit_ids = list(fill_deposit)
        max_add = max(0, need)
        if len(fill_connect) > max_add:
            fill_connect = fill_connect[:max_add]
        target = sorted(set(kept_target) | task_set | set(fill_connect))
        connect_new = [a for a in fill_connect if a not in task_set]
        if need > 0 and not connect_new and not deposit_ids:
            print(
                f"[WS-POOL] Phiên mới — thiếu {need} slot WS nhưng không chọn được nick "
                f"(list Hết Tiền/Đủ ngày hoặc Đang Chơi khác)",
                flush=True,
            )
        return WsSyncPlan(
            connect_all=connect_new,
            target=target,
            deposit_ids=deposit_ids,
            prune_removed=prune_removed,
            fill_connect_ids=connect_new,
        )

    # Resync định kỳ: giữ task đủ tiền; nick thiếu tiền → ngắt WS + nạp, không mở WS.
    task_keys = [
        str(x).strip() for x in (ws_task_ids or get_ws_task_accounts()) if str(x).strip()
    ]
    funded, underfunded = _split_ids_connect_deposit(task_keys, cfg)
    if underfunded:
        _log_ws_underfunded_to_deposit(cfg, underfunded, tag="Resync")
    prune_under = [a for a in underfunded if not is_ws_listener(a, cfg)]
    ex = set(prune_under) | ws_slots_exclude_ids(cfg)
    need = ws_slots_need_fill(cfg, task_ids=task_keys)
    if need > 0:
        _log_ws_slot_need(cfg, task_ids=task_keys, need=need, log_context="Resync định kỳ")
    fill_connect, fill_deposit = plan_ws_slot_fill(
        cfg,
        need,
        exclude=ex,
        prefer_dang_choi=True,
        log_context="",
    )
    deposit_ids = _merge_unique_account_ids(underfunded, fill_deposit)
    funded_set = set(funded)
    max_add = max(0, min_target - len(funded_set))
    if len(fill_connect) > max_add:
        fill_connect = fill_connect[:max_add]
    target = sorted(funded_set | set(fill_connect))
    connect_new = [a for a in fill_connect if a not in funded_set]
    if (
        set(task_keys) == set(target)
        and not deposit_ids
        and not prune_under
        and need <= 0
        and not connect_new
    ):
        return None

    return WsSyncPlan(
        connect_all=connect_new,
        target=target,
        deposit_ids=deposit_ids,
        prune_removed=prune_under,
        fill_connect_ids=connect_new,
    )


def _filter_ids_for_ws_deposit_schedule(
    cfg: dict[str, Any], account_ids: list[str]
) -> list[str]:
    """Bỏ nick đang nạp / DB đã đủ min_balance — tránh lên lịch nạp trùng."""
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


def schedule_fund_deposit_for_ws_shortage(
    cfg: dict[str, Any], deposit_ids: list[str], *, label: str = ""
) -> None:
    """Nạp nền — không chặn asyncio / BẮT ĐẦU PHIÊN."""
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
            for aid in ids:
                row = get_account(aid) or {}
                if account_deposit_in_flight(aid, cfg):
                    continue
                if account_balance_vnd(row) >= min_balance_for_ws(cfg):
                    clear_pending_ws_slot(aid)

    threading.Thread(target=_run, name=tag, daemon=True).start()


def fund_deposit_for_ws_shortage(
    cfg: dict[str, Any], deposit_ids: list[str]
) -> list[str]:
    """Nạp acc đã chọn (balance < min) — poll Hoàn tất rồi mở WS."""
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
        f"{', '.join(username_for_log(a) for a in ids)} (poll Hoàn tất → Đang Chơi; bù WS resync)",
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
    """Ngưỡng đủ tiền WS = bet_step_vnd (game_worker.min_balance_vnd chỉ override nếu set)."""
    gw = game_worker_cfg(cfg)
    if gw.get("min_balance_vnd") is not None:
        return int(gw.get("min_balance_vnd") or bet_step_vnd(cfg))
    return bet_step_vnd(cfg)


def side_total_vnd(cfg: dict[str, Any]) -> int:
    ab = cfg.get("auto_bet")
    if isinstance(ab, dict) and ab.get("side_total_vnd") is not None:
        return int(ab.get("side_total_vnd") or 100_000)
    return 100_000


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
        rep = refresh_account_balance_to_db(aid)
    except Exception as e:
        return None, db_before, str(e)
    if not rep.get("ok"):
        return None, db_before, str(rep.get("error") or "getBalance thất bại")
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
        rep = refresh_account_balance_to_db(aid, session, refresh=True)
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
            f"[WS-POOL] Đủ tiền — không nạp, chờ resync mở WS: {names}{extra}",
            flush=True,
        )
    open_ws_after_deposit_confirmed(funded, cfg)
    try:
        from xoso66_minigame_ws_worker import schedule_ws_pool_round_check

        schedule_ws_pool_round_check()
    except Exception:
        pass
    return funded


def account_needs_deposit_live(
    account_id: str,
    min_bal: int,
    *,
    log: bool = True,
) -> bool:
    """True nếu sau getBalance thực tế vẫn < min_bal (tránh nạp khi DB cũ = 0)."""
    user = username_for_log(account_id)
    live, db, err = sync_live_balance_vnd(account_id)
    if live is None:
        if log:
            print(
                f"[WS-POOL] {user} không getBalance ({err}) — "
                f"coi theo DB {db:,.0f}",
                flush=True,
            )
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
    from xoso66_startup_checks import run_startup_checks_for_pool

    run_startup_checks_for_pool(cfg, account_ids)
    mark_pool_startup_done()


def register_ws_connected(account_id: str) -> None:
    aid = str(account_id or "").strip()
    if not aid:
        return
    clear_pending_ws_slot(aid)
    with _connected_ws_lock:
        _connected_ws_ids.add(aid)


def unregister_ws_connected(account_id: str) -> None:
    aid = str(account_id or "").strip()
    if not aid:
        return
    with _connected_ws_lock:
        _connected_ws_ids.discard(aid)


def clear_all_ws_connected() -> None:
    """Ctrl+C / shutdown — tránh pool nghĩ nick vẫn đang connect."""
    with _connected_ws_lock:
        _connected_ws_ids.clear()


def get_connected_ws_accounts() -> list[str]:
    """Nick WS đã connect thành công (dùng cho gán cược — không chờ cả pool)."""
    with _connected_ws_lock:
        return sorted(_connected_ws_ids)


def _sort_key_balance_only(r: dict[str, Any]) -> tuple:
    return (
        -account_balance_vnd(r),
        str(r.get("username") or ""),
        str(r.get("id") or ""),
    )


def _sort_key_daily_bet_only(r: dict[str, Any]) -> tuple:
    return (
        -daily_bet_today_vnd(r),
        str(r.get("username") or ""),
        str(r.get("id") or ""),
    )


def sort_rows_ws_priority(
    rows: list[dict[str, Any]], cfg: dict[str, Any]
) -> list[dict[str, Any]]:
    """
    Đoạn 1: balance >= bet_step — balance cao → thấp.
    Đoạn 2: balance < bet_step — cược ngày cao → thấp.
    """
    step = bet_step_vnd(cfg)
    funded: list[dict[str, Any]] = []
    under: list[dict[str, Any]] = []
    for r in rows:
        if account_balance_vnd(r) >= step:
            funded.append(r)
        else:
            under.append(r)
    funded.sort(key=_sort_key_balance_only)
    under.sort(key=_sort_key_daily_bet_only)
    return funded + under


def iter_ws_priority_rows(
    cfg: dict[str, Any], *, exclude: set[str] | None = None
) -> list[dict[str, Any]]:
    """List ưu tiên: chỉ Hết Tiền / Đủ ngày (2 đoạn sort)."""
    ex = exclude or set()
    rows = [
        r
        for r in _pool_rows_ws_open_list(cfg)
        if str(r.get("id") or "") and str(r.get("id") or "") not in ex
    ]
    return sort_rows_ws_priority(rows, cfg)


def list_ws_priority_account_ids(
    cfg: dict[str, Any], *, exclude: set[str] | None = None
) -> list[str]:
    return [str(r["id"]) for r in iter_ws_priority_rows(cfg, exclude=exclude)]


def list_ws_priority_accounts_payload(cfg: dict[str, Any]) -> dict[str, Any]:
    """API: danh sách ưu tiên mở WS / nạp (2 đoạn)."""
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
        "allowed_statuses": sorted(WS_OPEN_LIST_STATUSES),
        **ws_target_occupied_counts(cfg),
        "account_ids": [x["id"] for x in items],
        "accounts": items,
    }


def round_start_balance_check_delay_sec(cfg: dict[str, Any]) -> float:
    gw = game_worker_cfg(cfg)
    if "round_start_balance_check_delay_sec" in gw:
        return max(0.0, float(gw.get("round_start_balance_check_delay_sec") or 0))
    return 10.0


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


def _disconnect_reason_label(row: dict[str, Any], cfg: dict[str, Any]) -> str:
    min_bal = min_balance_for_ws(cfg)
    daily = daily_bet_today_vnd(row)
    cap = daily_bet_cap_vnd(cfg)
    step = bet_step_vnd(cfg)
    if is_balance_too_low_for_ws(row, cfg):
        return f"balance<{min_bal:,}"
    if exceeds_ws_daily_cap(row, cfg):
        return f"cược ngày {daily:,.0f}+{step:,}>{cap:,}"
    return "?"


def _pending_bet_skip_ids() -> set[str]:
    try:
        from xoso66_auto_bet import pending_bet_account_ids

        return pending_bet_account_ids()
    except Exception:
        return set()


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
    """Mục tiêu WS: đủ tiền / không đang nạp; nick đang có task WS vẫn giữ (grace recheck)."""
    tasks = {str(x).strip() for x in (ws_task_ids or []) if str(x).strip()}
    pending = get_pending_ws_slot_ids()
    min_bal = min_balance_for_ws(cfg)
    out: list[str] = []
    for aid in account_ids:
        aid = str(aid).strip()
        if not aid:
            continue
        if aid in tasks:
            out.append(aid)
            continue
        if aid in pending or account_deposit_in_flight(aid, cfg):
            continue
        row = get_account(aid) or {}
        if account_balance_vnd(row) < min_bal:
            continue
        out.append(aid)
    return out


def _kept_for_ws_target(
    cfg: dict[str, Any],
    kept: list[str],
    recheck_low: list[str],
    *,
    ws_task_ids: list[str] | None = None,
) -> list[str]:
    """
    Nick giữ trong target đầu phiên:
    - Thiếu tiền (recheck): chỉ nếu vẫn còn task WS (grace 10s).
    - Đang pending slot / đang nạp: không mở WS, không chiếm target.
    """
    tasks = {str(x).strip() for x in (ws_task_ids or []) if str(x).strip()}
    pending = get_pending_ws_slot_ids()
    recheck_set = {str(x).strip() for x in recheck_low if str(x).strip()}
    out: list[str] = []
    for aid in kept:
        aid = str(aid).strip()
        if not aid:
            continue
        if aid in pending or account_deposit_in_flight(aid, cfg):
            continue
        if aid in recheck_set and aid not in tasks:
            continue
        out.append(aid)
    return out


def _accounts_db_low_for_ws_recheck(
    cfg: dict[str, Any], account_ids: list[str]
) -> list[str]:
    """Chỉ nick DB < min_balance (và không còn cược pending) — recheck sau delay."""
    skip = _pending_bet_skip_ids()
    out: list[str] = []
    for aid in account_ids:
        aid = str(aid).strip()
        if not aid or aid in skip:
            continue
        row = get_account(aid) or {}
        if is_balance_too_low_for_ws(row, cfg):
            out.append(aid)
    return out


def schedule_round_start_balance_prune(
    cfg: dict[str, Any], recheck_account_ids: list[str]
) -> None:
    """Sau delay: chỉ refresh/recheck nick DB đã báo thiếu tiền đầu phiên."""
    ids = [str(x).strip() for x in recheck_account_ids if str(x).strip()]
    if not ids:
        return
    delay = round_start_balance_check_delay_sec(cfg)
    if delay <= 0:

        def _run_now() -> None:
            min_bal = min_balance_for_ws(cfg)
            removed: list[str] = []
            for aid in ids:
                if is_ws_listener(aid, cfg):
                    continue
                live, db_before, _ok = _balance_vnd_after_site_refresh(aid)
                bal = (
                    live
                    if live is not None
                    else account_balance_vnd(get_account(aid) or {})
                )
                if bal >= min_bal:
                    if db_before < min_bal:
                        print(
                            f"[WS-POOL] {username_for_log(aid)} đủ tiền "
                            f"({bal:,.0f} >= {min_bal:,}) — giữ WS",
                            flush=True,
                        )
                    continue
                row = get_account(aid) or {}
                if should_disconnect_ws_at_round_start(row, cfg):
                    removed.append(aid)
            if removed:
                sync_status_for_ws_pool_change(cfg, leaving=removed, joining=[])
                request_ws_evict_and_resync(removed)

        threading.Thread(
            target=_run_now, name="ws-round-bal-prune", daemon=True
        ).start()
        return

    def _worker() -> None:
        from xoso66_shutdown import sleep_interruptible, stopping

        if not sleep_interruptible(delay) or stopping():
            return
        min_bal = min_balance_for_ws(cfg)
        skip_pending = _pending_bet_skip_ids()
        removed: list[str] = []
        for aid in ids:
            if stopping() or aid in skip_pending:
                continue
            if is_ws_listener(aid, cfg):
                continue
            if aid in get_pending_ws_slot_ids() or account_deposit_in_flight(aid, cfg):
                continue
            row_before = get_account(aid) or {}
            db_low_before = is_balance_too_low_for_ws(row_before, cfg)
            live_bal, db_before, refresh_ok = _balance_vnd_after_site_refresh(aid)
            if not refresh_ok:
                live2, _, err = sync_live_balance_vnd(aid)
                if live2 is not None:
                    live_bal = live2
                    refresh_ok = True
                elif err:
                    print(
                        f"[WS-POOL] {username_for_log(aid)} refresh balance "
                        f"(sau {delay:.0f}s): {err}",
                        flush=True,
                    )
            if live_bal is not None:
                bal_use = live_bal
            else:
                bal_use = account_balance_vnd(get_account(aid) or {})
            if bal_use >= min_bal:
                if db_low_before or (db_before < min_bal and refresh_ok):
                    print(
                        f"[WS-POOL] {username_for_log(aid)} sau {delay:.0f}s "
                        f"đủ tiền ({bal_use:,.0f} >= {min_bal:,}"
                        f"{f'; DB trước {db_before:,.0f}' if db_before < min_bal else ''}"
                        f") — giữ WS",
                        flush=True,
                    )
                continue
            row = get_account(aid) or {}
            if not should_disconnect_ws_at_round_start(row, cfg):
                continue
            removed.append(aid)
        if not removed:
            return
        details = ", ".join(
            f"{username_for_log(a)} ({_disconnect_reason_label(get_account(a) or {}, cfg)})"
            for a in removed
        )
        print(
            f"[WS-POOL] Sau {delay:.0f}s recheck balance — ngắt WS: {details}",
            flush=True,
        )
        sync_status_for_ws_pool_change(cfg, leaving=removed, joining=[])
        request_ws_evict_and_resync(removed)

    threading.Thread(
        target=_worker,
        name="ws-round-bal-prune",
        daemon=True,
    ).start()


def prune_ws_after_settlement(
    cfg: dict[str, Any], account_ids: list[str]
) -> None:
    """
    Sau KQ: ngắt WS ngay chỉ khi đủ cap ngày.
    Balance thấp trên DB (chưa cộng thưởng WS) → recheck sau delay, không ép Hết Tiền ngay.
    """
    open_ws = {
        str(x).strip()
        for x in get_ws_task_accounts() + get_connected_ws_accounts()
        if str(x).strip()
    }
    cap_remove: list[str] = []
    balance_recheck: list[str] = []
    for aid in account_ids:
        aid = str(aid).strip()
        if not aid or aid not in open_ws:
            continue
        row = get_account(aid) or {}
        if is_ws_listener(aid, cfg):
            continue
        if exceeds_ws_daily_cap(row, cfg):
            cap_remove.append(aid)
        elif is_balance_too_low_for_ws(row, cfg):
            balance_recheck.append(aid)
    if cap_remove:
        details = ", ".join(
            f"{username_for_log(a)} ({_disconnect_reason_label(get_account(a) or {}, cfg)})"
            for a in cap_remove
        )
        print(f"[WS-POOL] Sau KQ — ngắt WS (cap): {details}", flush=True)
        sync_status_for_ws_pool_change(cfg, leaving=cap_remove, joining=[])
        request_ws_evict_and_resync(cap_remove)
    if balance_recheck:
        _log_ws_underfunded_to_deposit(
            cfg, balance_recheck, tag="Sau KQ (recheck sau delay)"
        )
        schedule_round_start_balance_prune(cfg, balance_recheck)


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
    """Thứ tự nạp: priority trước, còn lại theo tổng cược ngày cao → thấp."""
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
    rest.sort(key=lambda x: (-x[1], -x[2], x[0]))
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


def _finalize_deposit_order_confirmed(
    aid: str, order_id: int, poll_rep: dict[str, Any]
) -> None:
    """Ghi DB đơn nạp Thành Công + bỏ cache (giống [DEPOSIT-POLL])."""
    item = poll_rep.get("item") if isinstance(poll_rep.get("item"), dict) else {}
    serial = str(poll_rep.get("serial_no") or item.get("serial_no") or "")
    from xoso66_deposit_orders_db import update_deposit_order

    update_deposit_order(
        int(order_id),
        status="Thành Công",
        serial_no=serial,
        site_status=1,
        site_status_formatted=str(item.get("status_formatted") or "Hoàn tất"),
    )
    u = username_for_log(aid)
    print(
        f"[WS-POOL] Đơn #{order_id} [{u}] Thành Công"
        + (f" serial={serial}" if serial else ""),
        flush=True,
    )
    try:
        from xoso66_auto_deposit import remove_from_deposit_cache

        remove_from_deposit_cache(aid)
    except Exception:
        pass


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
    since_ms = int(row.get("order_placed_at_ms") or rep.get("order_placed_at_ms") or 0)
    amount = int(row.get("amount") or rep.get("amount") or 0)
    ad = cfg.get("auto_deposit") if isinstance(cfg.get("auto_deposit"), dict) else {}
    interval = float(ad.get("poll_interval_sec") or 10)
    max_attempts = int(ad.get("poll_max_attempts") or 100)
    list_limit = int(ad.get("deposit_list_limit") or 10)

    print(
        f"[WS-POOL] Poll nạp #{order_id} {username_for_log(aid)} — {interval:.0f}s × {max_attempts} "
        f"(cần bên thứ 3 chuyển + lệnh Hoàn tất trên site)",
        flush=True,
    )
    try:
        session = ensure_session(aid, force_login=False)
    except Exception as e:
        print(f"[WS-POOL] Nạp poll {username_for_log(aid)}: không có session — {e}", flush=True)
        return False

    try:
        poll_rep = poll_deposit_until_confirmed(
            session,
            account_id=aid,
            amount_vnd=amount,
            since_ms=since_ms,
            poll_interval_sec=interval,
            max_attempts=max_attempts,
            list_limit=list_limit,
            order_id=oid,
        )
        if poll_rep.get("cancelled"):
            print(f"[WS-POOL] Poll nạp #{oid} {username_for_log(aid)} — đã hủy", flush=True)
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
        print(
            f"[WS-POOL] Nạp chưa Hoàn tất #{oid} {username_for_log(aid)}: "
            f"{poll_rep.get('error') or 'timeout'}",
            flush=True,
        )
        return False
    finally:
        end_deposit_poll(oid)


def open_ws_after_deposit_confirmed(
    account_ids: list[str], cfg: dict[str, Any] | None = None
) -> None:
    """
    Sau nạp Hoàn tất: chỉ chuyển «Đang Chơi».
    Mở WS do resync phiên / pool (tránh mở trùng với luồng nạp riêng).
    """
    ids = [str(x).strip() for x in account_ids if str(x).strip()]
    if not ids:
        return
    if cfg is None:
        from xoso66_config_util import load_config

        cfg = load_config()
    try:
        sync_status_for_ws_pool_change(cfg, joining=ids)
    except Exception as e:
        names = ", ".join(username_for_log(a) for a in ids)
        print(f"[WS-POOL] Không đổi status Đang Chơi ({names}): {e}", flush=True)


def fund_accounts_below_minimum(
    cfg: dict[str, Any],
    account_ids: list[str],
    *,
    priority_ids: list[str] | None = None,
    wait_confirm: bool | None = None,
    open_ws_on_confirm: bool = False,
) -> dict[str, Any]:
    """Nạp tuần tự acc balance < min_balance_vnd (nếu auto_deposit bật)."""
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

    try:
        for aid in order:
            if stopping():
                print("[WS-POOL] Dừng nạp (Ctrl+C)", flush=True)
                break
            if not can_create_deposit_order(aid):
                continue
            if not account_needs_deposit_live(aid, min_bal):
                restore_funded_ws_pool_accounts(cfg, [aid])
                continue
            _wait_deposit_slot()
            rep = perform_deposit(aid, amt, verbose=True)
            if rep.get("ok"):
                pending = str(rep.get("status") or rep.get("message") or "").upper()
                is_pending = "PENDING" in pending or "CHỜ" in pending
                confirmed = not is_pending
                if is_pending and should_wait:
                    confirmed = _wait_deposit_confirmed(cfg, aid, rep)
                elif is_pending and not should_wait:
                    confirmed = False
                if confirmed:
                    ok_ids.append(aid)
                    clear_pending_ws_slot(aid)
                    try:
                        from xoso66_session import refresh_account_balance_to_db

                        bal_rep = refresh_account_balance_to_db(aid)
                        if bal_rep.get("ok"):
                            print(
                                f"[WS-POOL] {username_for_log(aid)} balance sau nạp: "
                                f"{float(bal_rep.get('balance') or 0):,.0f}",
                                flush=True,
                            )
                    except Exception as e:
                        print(f"[WS-POOL] {username_for_log(aid)} refresh balance: {e}", flush=True)
                    if open_ws_on_confirm:
                        open_ws_after_deposit_confirmed([aid], cfg)
                else:
                    fail.append(
                        {
                            "id": aid,
                            "error": "pending_not_confirmed"
                            if is_pending
                            else "deposit_incomplete",
                        }
                    )
            else:
                err = str(rep.get("error") or "deposit_fail")
                print(f"[WS-POOL] Nạp FAIL {username_for_log(aid)}: {err}", flush=True)
                fail.append({"id": aid, "error": err})
    except KeyboardInterrupt:
        print("[WS-POOL] Hủy nạp (Ctrl+C)", flush=True)
        raise

    if fail:
        for f in fail:
            if f.get("error") in ("cache_or_pending", "pending_not_confirmed"):
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
    """Nạp ưu tiên một acc (poll Hoàn tất → mở WS lại nếu cần)."""
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

    min_target = ws_account_count(cfg)
    min_bal = min_balance_for_ws(cfg)
    ex: set[str] = set()

    dc_connect, dc_deposit = _classify_rows_connect_deposit(
        sort_rows_ws_priority(_pool_rows(cfg), cfg), cfg, exclude=ex
    )
    connect_ids = list(dc_connect[:min_target])
    deposit_ids = list(dc_deposit)
    ex |= set(connect_ids) | set(deposit_ids)

    need_fill = max(0, min_target - len(connect_ids))
    if need_fill > 0:
        fill_connect, fill_deposit = plan_ws_slot_fill(
            cfg,
            need_fill,
            exclude=ex,
            prefer_dang_choi=False,
            log_context="",
        )
        connect_ids.extend(fill_connect)
        for aid in fill_deposit:
            if aid not in deposit_ids:
                deposit_ids.append(aid)
        connect_ids = connect_ids[:min_target]

    deposit_ids = [a for a in deposit_ids if a not in set(connect_ids)]

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
    """Đầu phiên: resync pool (sau khi worker đã apply pool lần đầu)."""
    from xoso66_config_util import load_config

    cfg = load_config()
    if not cfg.get("game_worker_enabled"):
        return
    if not ws_round_sync_enabled():
        return
    try:
        from xoso66_minigame_ws_worker import schedule_ws_pool_round_check

        schedule_ws_pool_round_check()
    except Exception as e:
        print(f"[WS-POOL] Phiên mới — không gọi được resync WS: {e}", flush=True)


def register_ws_pool_round_handler() -> None:
    from xoso66_minigame_ws import register_round_start_handler

    register_round_start_handler(on_round_start_ws_pool)


def start_ws_balance_monitor(cfg: dict[str, Any]) -> threading.Thread | None:
    """Đã tắt — không nạp định kỳ nền (chỉ khởi động + đầu phiên)."""
    return None
