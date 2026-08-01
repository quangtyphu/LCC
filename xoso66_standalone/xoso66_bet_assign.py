# -*- coding: utf-8 -*-
"""
Gán acc + số tiền Tài/Xỉu theo chiến lược + trần cược ngày.

Chiến lược 1: acc tổng cược ngày cao nhất (trong số acc còn chỗ cap) → mức cao nhất
  vừa cap; còn lại random. Acc đầu bảng hết room vẫn đánh phiên bằng acc khác.
  daily >= cap-step → ngắt WS, thay nick.
Chiến lược 2: acc cược ngày thấp → cao (bằng nhau: balance cao trước); mỗi acc một lệnh —
  trong mức còn lại duyệt to → nhỏ, khớp mức đầu tiên vừa cap + số dư; sang acc kế tiếp.
Chiến lược 3: mỗi phiên 1 cặp — acc #1 vs #2 (số dư thấp→cao), cùng mức Tài/Xỉu (dồn tiền).
"""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass
from typing import Any

from xoso66_accounts_db import (
    STATUS_DANG_CHOI,
    daily_bet_today_vnd,
    list_accounts_by_status,
)
from xoso66_bet_daily import resolve_daily_bets
from xoso66_bet_split import split_side_total
from xoso66_jackpot_picker import resolve_side_total_vnd
from xoso66_proxy import resolve_proxy


@dataclass
class BetSlot:
    account_id: str
    username: str
    side: str  # tai | xiu
    amount_vnd: int


def sort_slots_by_amount_desc(slots: list[BetSlot]) -> list[BetSlot]:
    """Sắp xếp lệnh cược mức cao → thấp (bằng nhau: username ổn định)."""
    return sorted(
        slots,
        key=lambda s: (-int(s.amount_vnd), str(s.username or s.account_id)),
    )


def _auto_bet_cfg(cfg: dict) -> dict:
    raw = cfg.get("auto_bet")
    return raw if isinstance(raw, dict) else {}


def _balance_vnd(row: dict[str, Any]) -> float:
    try:
        return float(row.get("balance") or 0)
    except (TypeError, ValueError):
        return 0.0


def _daily_cap_vnd(acfg: dict) -> int:
    return int(acfg.get("daily_bet_cap_vnd") or 890_000)


def _max_bet_per_user_vnd(acfg: dict) -> int:
    """Mỗi acc tối đa một lệnh (0 = không giới hạn)."""
    try:
        return int(acfg.get("max_bet_per_user_vnd") or 0)
    except (TypeError, ValueError):
        return 0


STRATEGY_LABELS: dict[int, str] = {
    1: "cược ngày cao (còn chỗ cap) → mức lớn nhất vừa cap; còn lại random; đủ cap đổi WS",
    2: "cược ngày thấp→cao (bằng nhau: balance cao trước); mỗi acc 1 mức còn lại (to→nhỏ)",
    3: "1 cặp/phiên — duyệt số dư cao→thấp, ghép liền kề floor gap≤XXX, cùng mức Tài/Xỉu (dồn tiền)",
}

CONSOLIDATE_WITHDRAW_REASON = "đạt ngưỡng rút strategy 3"
CONSOLIDATE_LOSE_REASON = "strategy 3 thua hết tiền"


def _check_balance_enabled(acfg: dict) -> bool:
    return bool(acfg.get("check_balance", False))


def _assign_ws_pool_only(acfg: dict) -> bool:
    """
    Chiến lược 2: gán trên mọi acc «Đang Chơi» đủ proxy (HTTP placeOrder),
    không giới hạn nick đang mở WS — tránh bỏ sót acc cược ngày thấp.
    Chiến lược 1 / 3: mặc định chỉ pool WS (assign_ws_pool_only=true).
    """
    if int(acfg.get("assign_strategy") or 1) == 2:
        return False
    return bool(acfg.get("assign_ws_pool_only", True))


def is_assign_strategy_3(acfg: dict | None = None) -> bool:
    if acfg is None:
        acfg = {}
    return int(acfg.get("assign_strategy") or 1) == 3


def consolidate_min_withdraw_vnd(acfg: dict | None = None) -> int:
    """Ngưỡng dừng cược + hẹn rút (mặc định 300k, fallback auto_mission_reward)."""
    if acfg is None:
        acfg = {}
    explicit = int(acfg.get("consolidate_min_withdraw_vnd") or 0)
    if explicit > 0:
        return explicit
    try:
        from xoso66_config_util import load_config

        mr = load_config().get("auto_mission_reward")
        if isinstance(mr, dict) and mr.get("min_withdraw_vnd"):
            return int(mr.get("min_withdraw_vnd") or 300_000)
    except Exception:
        pass
    return 300_000


def consolidate_no_deposit_enabled(acfg: dict | None = None) -> bool:
    """Strategy 3: mặc định không nạp (mọi acc)."""
    if acfg is None:
        acfg = {}
    if not is_assign_strategy_3(acfg):
        return False
    if "consolidate_no_deposit" in acfg:
        return bool(acfg.get("consolidate_no_deposit"))
    return True


def consolidate_pair_max_gap_vnd(acfg: dict | None = None) -> int:
    """Strategy 3: chênh tối đa giữa floor(bet_step) của 2 acc liền kề (0 = floor phải bằng)."""
    if acfg is None:
        acfg = {}
    return max(0, int(acfg.get("consolidate_pair_max_gap_vnd") or 0))


def consolidate_withdraw_delay_sec(acfg: dict | None = None) -> float:
    """Strategy 3: chờ trước khi rút sau Đủ ngày (mặc định 420s = 7p)."""
    if acfg is None:
        acfg = {}
    try:
        sec = float(acfg.get("consolidate_withdraw_delay_sec") or 420)
    except (TypeError, ValueError):
        sec = 420.0
    return max(60.0, sec)


def consolidate_min_ws_balance_vnd(acfg: dict | None = None) -> int:
    """Strategy 3: sàn mở WS / fill slot (0 = dùng bet_step như cũ)."""
    if acfg is None:
        acfg = {}
    if not is_assign_strategy_3(acfg):
        return 0
    try:
        return max(0, int(acfg.get("consolidate_min_ws_balance_vnd") or 0))
    except (TypeError, ValueError):
        return 0


def consolidate_blocks_deposit(account_id: str) -> bool:
    """True nếu strategy 3 đang chặn mọi lệnh nạp."""
    _ = account_id
    from xoso66_config_util import load_config

    acfg = _auto_bet_cfg(load_config())
    return consolidate_no_deposit_enabled(acfg)


def consolidate_skips_underfunded_ws(acfg: dict | None = None) -> bool:
    """Strategy 3: không mở WS và không lên lịch nạp acc < sàn WS (min_balance_for_ws)."""
    return is_assign_strategy_3(acfg)


def assign_match_mode(acfg: dict | None = None) -> int:
    """
    0 = khớp được lệnh nào thì cược lệnh đó (không hủy phiên vì dư mức).
    1 = phải khớp hết mức Tài/Xỉu mới cược (mặc định).
    """
    if acfg is None:
        acfg = {}
    try:
        mode = int(acfg.get("assign_match_mode", 1))
    except (TypeError, ValueError):
        mode = 1
    return 0 if mode == 0 else 1


ASSIGN_MATCH_MODE_LABELS: dict[int, str] = {
    0: "khớp được lệnh nào thì cược (Tài/Xỉu chung pool, không bắt đủ 2 bên)",
    1: "phải khớp hết mức mới cược",
}


def _format_remaining_levels(remaining: list[tuple[int, str]]) -> str:
    parts = [f"{s} {a:,}" for a, s in remaining[:6]]
    extra = f"… +{len(remaining) - 6}" if len(remaining) > 6 else ""
    return f"dư {len(remaining)} mức cược ({', '.join(parts)}{extra})"


def _min_balance_for_assign(cfg: dict, acfg: dict) -> int:
    explicit = int(acfg.get("min_balance_vnd") or 0)
    if explicit > 0:
        return explicit
    return int(acfg.get("bet_step_vnd") or 10_000)


def _assign_min_remainder_vnd(cfg: dict, acfg: dict) -> int:
    """Số dư tối thiểu còn sau cược — mặc định 0 (chỉ cần đủ tiền lệnh)."""
    if not _check_balance_enabled(acfg):
        return 0
    explicit = int(acfg.get("min_balance_remainder_vnd") or 0)
    if explicit > 0:
        return explicit
    return 0


def _min_balance_pool_entry(cfg: dict, acfg: dict) -> int:
    """Ngưỡng vào pool gán cược (= min WS, thường 10k) — không cộng buffer lần 2."""
    explicit = int(acfg.get("min_balance_pool_vnd") or 0)
    if explicit > 0:
        return explicit
    try:
        from xoso66_ws_pool import min_balance_for_ws

        return min_balance_for_ws(cfg)
    except Exception:
        return _min_balance_for_assign(cfg, acfg)


def _balance_fits_bet(balance: float, amount_vnd: int, remainder_vnd: int) -> bool:
    if remainder_vnd <= 0:
        return balance >= int(amount_vnd)
    return balance >= int(amount_vnd) + int(remainder_vnd)


def _daily_for_cap(row: dict[str, Any], daily: dict[str, float]) -> float:
    """Cược ngày để check cap (ưu tiên cột DB hôm nay)."""
    from_row = daily_bet_today_vnd(row)
    aid = str(row.get("id") or "")
    from_map = float(daily.get(aid, 0) or 0)
    if from_row <= 0 and from_map > 0:
        return from_map
    return from_row


def _ws_ids_for_assign() -> set[str]:
    """Nick WS đã connect (ưu tiên); fallback task đang mở socket."""
    from xoso66_ws_pool import get_connected_ws_accounts, get_ws_task_accounts

    ids = {str(x) for x in get_connected_ws_accounts() if str(x).strip()}
    if ids:
        return ids
    return {str(x) for x in get_ws_task_accounts() if str(x).strip()}


def _build_assign_pool(cfg: dict, acfg: dict) -> tuple[list[dict[str, Any]], str]:
    """
    Pool gán cược: status + proxy (+ WS nếu bật).
    Balance và cap ngày chỉ kiểm tra khi gán từng mức (sau khi đã chia tiền).
    """
    status = str(acfg.get("account_status") or STATUS_DANG_CHOI)
    rows = list_accounts_by_status(status)
    pool: list[dict[str, Any]] = []
    for r in rows:
        if resolve_proxy(r):
            pool.append(r)

    if _assign_ws_pool_only(acfg):
        ws_ids = _ws_ids_for_assign()
        if not ws_ids:
            return [], "chưa có nick trong pool WS — chờ worker mở WS"
        pool = [r for r in pool if str(r.get("id") or "") in ws_ids]
        if not pool:
            return [], "không có acc «Đang Chơi» trong pool WS"

    return pool, ""


def is_assign_pool_empty(cfg: dict) -> bool:
    """Pool gán cược (Đang Chơi + WS nếu bật) trống hoặc lỗi chờ WS."""
    pool, err = _build_assign_pool(cfg, _auto_bet_cfg(cfg))
    return bool(err) or not pool


def _daily_of(aid: str, daily: dict[str, float]) -> float:
    """Chỉ đọc tổng cược ngày đã ghi DB (sau các lệnh thành công trước đó)."""
    return float(daily.get(aid, 0))


def _bet_fits_daily_cap(daily_vnd: float, amount_vnd: int, cap_vnd: int) -> bool:
    """Tổng cược ngày sau lệnh phải < cap (không được >= ngưỡng)."""
    return daily_vnd + int(amount_vnd) < int(cap_vnd)


def _daily_ws_limit(acfg: dict) -> int:
    cap = _daily_cap_vnd(acfg)
    step = int(acfg.get("bet_step_vnd") or 10_000)
    return max(0, cap - step)


def _largest_fitting_amount(
    daily_vnd: float,
    amounts: list[int],
    cap_vnd: int,
    *,
    balance_vnd: float | None = None,
    remainder_vnd: int = 0,
) -> int | None:
    fitting = sorted(
        {
            int(a)
            for a in amounts
            if _bet_fits_daily_cap(daily_vnd, int(a), cap_vnd)
            and (
                balance_vnd is None
                or _balance_fits_bet(balance_vnd, int(a), remainder_vnd)
            )
        },
        reverse=True,
    )
    return fitting[0] if fitting else None


def _evict_ws_if_daily_exhausted(
    cfg: dict,
    pool: list[dict[str, Any]],
    daily: dict[str, float],
    acfg: dict,
) -> list[dict[str, Any]]:
    limit = _daily_ws_limit(acfg)
    exhausted: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for row in pool:
        if _daily_for_cap(row, daily) >= limit:
            exhausted.append(row)
        else:
            kept.append(row)
    if exhausted and _assign_ws_pool_only(acfg):
        from xoso66_ws_pool import mark_daily_cap_status, request_ws_evict_and_resync

        aids = [str(r["id"]) for r in exhausted]
        mark_daily_cap_status(aids, cfg)
        request_ws_evict_and_resync(aids)
        names = ", ".join(
            str(r.get("username") or r["id"]) for r in exhausted
        )
        print(
            f"[BET-ASSIGN] Ngắt WS — cược ngày >= {limit:,} "
            f"(cap - bet_step), thay nick: {names}",
            flush=True,
        )
    return kept


def _priority_users_from_cfg(cfg: dict) -> list[str]:
    """Đọc PRIORITY_USERS từ config gốc hoặc auto_bet."""
    lst = cfg.get("PRIORITY_USERS") or cfg.get("auto_bet", {}).get("PRIORITY_USERS") or []
    return [str(u).strip() for u in lst if str(u).strip()]


def _pick_candidate(
    candidates: list[dict[str, Any]],
    *,
    strategy: int,
    daily: dict[str, float],
) -> dict[str, Any]:
    key = lambda r: (  # noqa: E731
        _daily_for_cap(r, daily),
        -_balance_vnd(r),
        str(r.get("username") or r["id"]),
    )
    if strategy == 2:
        return min(candidates, key=key)
    return max(
        candidates,
        key=lambda r: (
            _daily_for_cap(r, daily),
            _balance_vnd(r),
            str(r.get("username") or r["id"]),
        ),
    )


def _pool_sorted_by_daily_asc(
    pool: list[dict[str, Any]], daily: dict[str, float]
) -> list[dict[str, Any]]:
    """
    Chiến lược 2: cược ngày thấp → cao.
    Bằng nhau → balance cao hơn trước.
    Vẫn bằng → username (ổn định).
    """
    return sorted(
        pool,
        key=lambda r: (
            _daily_for_cap(r, daily),
            -_balance_vnd(r),
            str(r.get("username") or r["id"]),
        ),
    )


def _pool_sorted_by_daily_desc(
    pool: list[dict[str, Any]], daily: dict[str, float]
) -> list[dict[str, Any]]:
    """Cược ngày cao → thấp (slot mức lớn strategy 1, fallback LC79)."""
    return sorted(
        pool,
        key=lambda r: (
            -_daily_for_cap(r, daily),
            -_balance_vnd(r),
            str(r.get("username") or r["id"]),
        ),
    )


def _find_largest_slot_user(
    search_pool: list[dict[str, Any]],
    daily: dict[str, float],
    all_amounts: list[int],
    *,
    cap_vnd: int,
    check_balance: bool,
    min_remainder_vnd: int,
) -> tuple[dict[str, Any] | None, int | None]:
    """Acc đầu tiên trong pool gánh được mức lớn nhất (cap + balance)."""
    for row in search_pool:
        bal_try = _balance_vnd(row) if check_balance else None
        amt_try = _largest_fitting_amount(
            _daily_for_cap(row, daily),
            all_amounts,
            cap_vnd,
            balance_vnd=bal_try,
            remainder_vnd=min_remainder_vnd if check_balance else 0,
        )
        if amt_try is not None:
            return row, int(amt_try)
    return None, None


def _pool_priority_then_random(
    pool: list[dict[str, Any]],
    priority_users: list[str],
) -> list[dict[str, Any]]:
    """
    Sắp xếp pool: PRIORITY_USERS (theo thứ tự config) trước,
    còn lại shuffle random (chỉ dùng khi không có thứ tự chiến lược).
    """
    prio_set = set(priority_users)
    prio_order = {u: i for i, u in enumerate(priority_users)}
    priority_pool = sorted(
        [r for r in pool if str(r.get("username") or r["id"]) in prio_set],
        key=lambda r: prio_order.get(str(r.get("username") or r["id"]), 9999),
    )
    rest = [r for r in pool if str(r.get("username") or r["id"]) not in prio_set]
    random.shuffle(rest)
    return priority_pool + rest


def _pool_priority_then_by_strategy(
    pool: list[dict[str, Any]],
    priority_users: list[str],
    daily: dict[str, float],
    *,
    strategy: int,
) -> list[dict[str, Any]]:
    """
    PRIORITY_USERS trước (theo thứ tự config); phần còn lại theo chiến lược:
    2 = cược ngày thấp→cao; 1 = cược ngày cao→thấp (bằng nhau: balance cao).
    """
    prio_set = set(priority_users)
    prio_order = {u: i for i, u in enumerate(priority_users)}
    priority_pool = sorted(
        [r for r in pool if str(r.get("username") or r["id"]) in prio_set],
        key=lambda r: prio_order.get(str(r.get("username") or r["id"]), 9999),
    )
    rest = [r for r in pool if str(r.get("username") or r["id"]) not in prio_set]
    if strategy == 2:
        rest = _pool_sorted_by_daily_asc(rest, daily)
    else:
        rest = _pool_sorted_by_daily_desc(rest, daily)
    return priority_pool + rest


def _row_fits_order(
    row: dict[str, Any],
    daily: dict[str, float],
    amount_vnd: int,
    *,
    cap_vnd: int,
    check_balance: bool,
    min_remainder_vnd: int,
) -> bool:
    if not _bet_fits_daily_cap(
        _daily_for_cap(row, daily), amount_vnd, cap_vnd
    ):
        return False
    if check_balance and not _balance_fits_bet(
        _balance_vnd(row), amount_vnd, min_remainder_vnd
    ):
        return False
    return True


def _assign_strategy_2(
    pool: list[dict[str, Any]],
    daily: dict[str, float],
    amounts_tai: list[int],
    amounts_xiu: list[int],
    *,
    cap_vnd: int,
    check_balance: bool,
    min_remainder_vnd: int = 0,
    match_mode: int = 1,
    priority_users: list[str] | None = None,
) -> tuple[list[BetSlot], str, str]:
    """
    PRIORITY_USERS: thử acc đó trước (theo thứ tự config).
    Phần còn lại: cược ngày thấp → cao (không random).
    Mỗi acc tối đa một lệnh/phiên: trong các mức còn lại, duyệt to → nhỏ, khớp mức đầu tiên
    vừa cap + số dư; acc không khớp mức nào thì bỏ qua, sang acc kế.
    """
    remaining: list[tuple[int, str]] = [
        *((int(a), "tai") for a in amounts_tai),
        *((int(a), "xiu") for a in amounts_xiu),
    ]
    slots: list[BetSlot] = []

    if priority_users:
        ordered_pool = _pool_priority_then_by_strategy(
            pool, priority_users, daily, strategy=2
        )
    else:
        ordered_pool = _pool_sorted_by_daily_asc(pool, daily)

    for row in ordered_pool:
        if not remaining:
            break
        remaining.sort(key=lambda x: (-int(x[0]), x[1]))
        pick_i: int | None = None
        for i, (amount_vnd, side) in enumerate(remaining):
            if _row_fits_order(
                row,
                daily,
                amount_vnd,
                cap_vnd=cap_vnd,
                check_balance=check_balance,
                min_remainder_vnd=min_remainder_vnd,
            ):
                pick_i = i
                break
        if pick_i is None:
            continue

        amount_vnd, side = remaining.pop(pick_i)
        aid = str(row["id"])
        slots.append(
            BetSlot(
                account_id=aid,
                username=str(row.get("username") or aid),
                side=side,
                amount_vnd=int(amount_vnd),
            )
        )

    if remaining:
        note = (
            f"{_format_remaining_levels(remaining)} "
            f"— pool {len(pool)} acc, đã gán {len(slots)}; cap ngày {cap_vnd:,}"
        )
        if match_mode == 0 and slots:
            return slots, "", note
        return (
            [],
            f"hủy phiên — {note}",
            "",
        )
    return slots, "", ""


def _candidates_for_order(
    pool: list[dict[str, Any]],
    used: set[str],
    daily: dict[str, float],
    amount_vnd: int,
    *,
    cap_vnd: int,
    check_balance: bool,
    min_remainder_vnd: int = 0,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in pool:
        aid = str(row["id"])
        if aid in used:
            continue
        if not _bet_fits_daily_cap(_daily_for_cap(row, daily), amount_vnd, cap_vnd):
            continue
        if check_balance and not _balance_fits_bet(
            _balance_vnd(row), amount_vnd, min_remainder_vnd
        ):
            continue
        out.append(row)
    return out


def _pick_strat_side(
    strat_amt: int, tai_amts: list[int], xiu_amts: list[int]
) -> str:
    """Chọn bên cho slot ưu tiên — nếu cả hai bên cùng có mức, ưu tiên bên còn nhiều lệnh."""
    amt = int(strat_amt)
    in_tai = amt in {int(a) for a in tai_amts}
    in_xiu = amt in {int(a) for a in xiu_amts}
    if in_tai and not in_xiu:
        return "tai"
    if in_xiu and not in_tai:
        return "xiu"
    if len(tai_amts) >= len(xiu_amts):
        return "tai"
    return "xiu"


def _remove_one_amount(amounts: list[int], amount: int) -> list[int]:
    """Bỏ một mức đã gán slot ưu tiên — tránh trừ trùng làm mất tổng bên."""
    out: list[int] = []
    removed = False
    for a in amounts:
        if not removed and int(a) == int(amount):
            removed = True
            continue
        out.append(int(a))
    return out


def _assign_side_orders(
    pool: list[dict[str, Any]],
    daily: dict[str, float],
    amounts: list[int],
    side: str,
    used: set[str],
    *,
    strategy: int,
    cap_vnd: int,
    check_balance: bool,
    min_remainder_vnd: int = 0,
    match_mode: int = 1,
    priority_users: list[str] | None = None,
) -> tuple[list[BetSlot], str, str]:
    slots: list[BetSlot] = []
    skipped: list[tuple[int, str]] = []
    prio_set: set[str] = set(priority_users or [])
    prio_order: dict[str, int] = {u: i for i, u in enumerate(priority_users or [])}
    for amount_vnd in sorted((int(a) for a in amounts), reverse=True):
        candidates = _candidates_for_order(
            pool,
            used,
            daily,
            amount_vnd,
            cap_vnd=cap_vnd,
            check_balance=check_balance,
            min_remainder_vnd=min_remainder_vnd,
        )
        if not candidates:
            if match_mode == 0:
                skipped.append((int(amount_vnd), side))
                continue
            return (
                [],
                f"hủy phiên — không gán được {side} {amount_vnd:,} VND "
                f"(balance/cap; pool {len(pool)} acc, đã dùng {len(used)}; "
                f"cap ngày {cap_vnd:,})",
                "",
            )
        # Nếu có PRIORITY_USERS: ưu tiên chọn trong nhóm đó trước cho mọi mức (nhất là mức lớn).
        # Fallback: giữ hành vi cũ theo strategy.
        chosen: dict[str, Any] | None = None
        if prio_set:
            prio_candidates = [
                r for r in candidates if str(r.get("username") or r["id"]) in prio_set
            ]
            if prio_candidates:
                chosen = min(
                    prio_candidates,
                    key=lambda r: prio_order.get(str(r.get("username") or r["id"]), 9999),
                )
        if chosen is None:
            if strategy == 1:
                chosen = random.choice(candidates)
            else:
                chosen = _pick_candidate(candidates, strategy=strategy, daily=daily)
        aid = str(chosen["id"])
        used.add(aid)
        slots.append(
            BetSlot(
                account_id=aid,
                username=str(chosen.get("username") or aid),
                side=side,
                amount_vnd=int(amount_vnd),
            )
        )
    if skipped:
        note = (
            f"{_format_remaining_levels(skipped)} "
            f"— pool {len(pool)} acc, đã gán {len(slots)} bên {side}; cap ngày {cap_vnd:,}"
        )
        if match_mode == 0:
            return slots, "", note
    return slots, "", ""


def _pool_sorted_by_balance_desc(pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        pool,
        key=lambda r: (
            -_balance_vnd(r),
            str(r.get("username") or r["id"]),
        ),
    )


def _pool_sorted_by_balance_asc(pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        pool,
        key=lambda r: (
            _balance_vnd(r),
            str(r.get("username") or r["id"]),
        ),
    )


def _floor_balance(balance_vnd: int, step: int) -> int:
    """Làm tròn xuống bội bet_step — dùng trước khi so gap và tính mức cược."""
    if step <= 0:
        return int(balance_vnd)
    return (int(balance_vnd) // step) * step


def _consolidate_pair_amount(
    row_a: dict[str, Any],
    row_b: dict[str, Any],
    daily: dict[str, float],
    *,
    step: int,
    cap_vnd: int,
    max_per_user: int,
    ignore_daily_cap: bool = False,
) -> int | None:
    """Mức cược chung: floor(min balance / step) * step; strategy 3 bỏ qua cap ngày."""
    bal = min(_balance_vnd(row_a), _balance_vnd(row_b))
    if bal < step:
        return None
    amount = (int(bal) // step) * step
    if max_per_user > 0:
        amount = min(amount, max_per_user)
        amount = (amount // step) * step
    if amount < step:
        return None
    for row in (row_a, row_b):
        if not ignore_daily_cap and not _bet_fits_daily_cap(
            _daily_for_cap(row, daily), amount, cap_vnd
        ):
            return None
        if not _balance_fits_bet(_balance_vnd(row), amount, 0):
            return None
    return int(amount)


def _pick_strategy_3_gap_pair(
    eligible: list[dict[str, Any]],
    *,
    step: int,
    max_gap_vnd: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, int | None]:
    """
    Sort số dư cao→thấp; duyệt cặp liền kề.
    Floor xuống bội step trước khi so gap; gap <= max_gap_vnd thì cược min(floor).
    """
    ordered = _pool_sorted_by_balance_desc(eligible)
    if len(ordered) < 2:
        return None, None, None
    for i in range(len(ordered) - 1):
        row_high = ordered[i]
        row_low = ordered[i + 1]
        floor_high = _floor_balance(_balance_vnd(row_high), step)
        floor_low = _floor_balance(_balance_vnd(row_low), step)
        gap = abs(floor_high - floor_low)
        if gap > max_gap_vnd:
            continue
        amount = min(floor_high, floor_low)
        if amount < step:
            continue
        if not _balance_fits_bet(_balance_vnd(row_high), amount, 0):
            continue
        if not _balance_fits_bet(_balance_vnd(row_low), amount, 0):
            continue
        return row_high, row_low, int(amount)
    return None, None, None


def _assign_strategy_3_pair(
    pool: list[dict[str, Any]],
    daily: dict[str, float],
    *,
    step: int,
    min_withdraw_vnd: int,
    max_gap_vnd: int = 0,
) -> tuple[list[BetSlot], str, str]:
    """
    Mỗi phiên: duyệt số dư cao→thấp, ghép cặp liền kề có floor gap ≤ max_gap_vnd.
    """
    _ = daily
    eligible = [
        r
        for r in pool
        if _balance_vnd(r) >= step and _balance_vnd(r) <= min_withdraw_vnd
    ]
    if len(eligible) < 2:
        return (
            [],
            f"hủy phiên — strategy 3 cần ≥2 acc (số dư {step:,}–{min_withdraw_vnd:,}; "
            f"pool {len(pool)} acc, đủ điều kiện {len(eligible)})",
            "",
        )

    row_high, row_low, amount = _pick_strategy_3_gap_pair(
        eligible,
        step=step,
        max_gap_vnd=max_gap_vnd,
    )

    if row_high is None or row_low is None or amount is None:
        return (
            [],
            f"hủy phiên — strategy 3 không tìm được cặp liền kề (floor gap ≤ {max_gap_vnd:,}; "
            f"pool {len(pool)} acc, đủ điều kiện {len(eligible)})",
            "",
        )

    bal_high = _balance_vnd(row_high)
    bal_low = _balance_vnd(row_low)
    floor_high = _floor_balance(bal_high, step)
    floor_low = _floor_balance(bal_low, step)
    gap = abs(floor_high - floor_low)
    user_high = str(row_high.get("username") or row_high["id"])
    user_low = str(row_low.get("username") or row_low["id"])
    print(
        f"[STRAT3] {user_high} ({bal_high:,.0f}) vs {user_low} ({bal_low:,.0f}) "
        f"(floor {floor_high:,}/{floor_low:,}, gap={gap:,} ≤ {max_gap_vnd:,}) "
        f"— mức {amount:,} Tài/Xỉu",
        flush=True,
    )
    slots = [
        BetSlot(
            account_id=str(row_low["id"]),
            username=user_low,
            side="tai",
            amount_vnd=int(amount),
        ),
        BetSlot(
            account_id=str(row_high["id"]),
            username=user_high,
            side="xiu",
            amount_vnd=int(amount),
        ),
    ]
    return slots, "", ""


def verify_pair_balanced(slots: list[BetSlot]) -> str | None:
    """Strategy 3: Tài và Xỉu cùng tổng, đúng 1 acc mỗi bên."""
    if len(slots) != 2:
        return f"strategy 3 cần đúng 2 lệnh, có {len(slots)}"
    tai = [s for s in slots if s.side == "tai"]
    xiu = [s for s in slots if s.side == "xiu"]
    if len(tai) != 1 or len(xiu) != 1:
        return "strategy 3 cần 1 acc Tài và 1 acc Xỉu"
    if int(tai[0].amount_vnd) != int(xiu[0].amount_vnd):
        return (
            f"strategy 3 mức không cân: Tài {tai[0].amount_vnd:,} "
            f"≠ Xỉu {xiu[0].amount_vnd:,}"
        )
    return None


def _apply_consolidate_round_aftermath(account_ids: list[str], cfg: dict) -> None:
    from xoso66_accounts_db import (
        STATUS_DU_NGAY,
        STATUS_HET_TIEN,
        get_account,
        set_account_status,
    )
    from xoso66_sessions_io import load_sessions
    from xoso66_session import refresh_account_balance_to_db
    from xoso66_ws_pool import min_balance_for_ws, request_ws_evict_and_resync

    acfg = _auto_bet_cfg(cfg)
    min_ws = min_balance_for_ws(cfg)
    min_withdraw = consolidate_min_withdraw_vnd(acfg)
    sessions = load_sessions()
    evict: list[str] = []

    for aid in account_ids:
        aid = str(aid).strip()
        if not aid:
            continue
        row = get_account(aid) or {}
        bal = _balance_vnd(row)
        sess = sessions.get(aid)
        if sess:
            try:
                rep = refresh_account_balance_to_db(aid, sess, refresh=True)
                if rep.get("ok") and rep.get("balance") is not None:
                    bal = float(rep["balance"])
            except Exception as e:
                print(f"[STRAT3] {aid}: refresh balance: {e}", flush=True)

        if bal < min_ws:
            if set_account_status(aid, STATUS_HET_TIEN, reason=CONSOLIDATE_LOSE_REASON):
                evict.append(aid)
        elif bal > min_withdraw:
            if set_account_status(
                aid, STATUS_DU_NGAY, reason=CONSOLIDATE_WITHDRAW_REASON
            ):
                evict.append(aid)

    if evict:
        request_ws_evict_and_resync(evict)


def schedule_consolidate_round_aftermath(slots: list[BetSlot], cfg: dict) -> None:
    """Sau KQ phiên strategy 3: chờ balance sync rồi Hết Tiền / Đủ ngày."""
    acfg = _auto_bet_cfg(cfg)
    if not is_assign_strategy_3(acfg):
        return
    aids = [str(s.account_id).strip() for s in slots if str(s.account_id).strip()]
    if not aids:
        return
    delay = float(acfg.get("result_balance_wait_sec") or 12)

    def _worker() -> None:
        from xoso66_shutdown import sleep_interruptible, stopping

        if not sleep_interruptible(delay) or stopping():
            return
        try:
            _apply_consolidate_round_aftermath(aids, cfg)
        except Exception as e:
            print(f"[STRAT3] aftermath: {e}", flush=True)

    threading.Thread(
        target=_worker,
        name="strat3-aftermath",
        daemon=True,
    ).start()


def _assign_by_strategy(
    pool: list[dict[str, Any]],
    daily: dict[str, float],
    amounts_tai: list[int],
    amounts_xiu: list[int],
    *,
    strategy: int,
    cap_vnd: int,
    check_balance: bool,
    min_remainder_vnd: int = 0,
    match_mode: int = 1,
    priority_users: list[str] | None = None,
) -> tuple[list[BetSlot], str, str]:
    """
    Chiến lược 1 (full): gán từng bên Tài rồi Xỉu — mỗi acc tối đa 1 lệnh/phiên.
    assign_match_mode=0 hoặc chiến lược 2: gộp mức Tài+Xỉu, không bắt phải có cả hai bên.
    Nếu có PRIORITY_USERS: thử acc đó trước cho slot mức lớn; không khớp → fallback
    như không có priority (acc cược ngày cao nhất), giống LC79 chiaTien_Acc strategy 1.
    """
    if strategy == 3:
        return [], "strategy 3 dùng _assign_strategy_3_pair riêng", ""

    if strategy == 2 or match_mode == 0:
        return _assign_strategy_2(
            pool,
            daily,
            [int(a) for a in amounts_tai],
            [int(a) for a in amounts_xiu],
            cap_vnd=cap_vnd,
            check_balance=check_balance,
            min_remainder_vnd=min_remainder_vnd,
            match_mode=match_mode,
            priority_users=priority_users,
        )

    used: set[str] = set()
    slots_out: list[BetSlot] = []
    tai_amts = [int(a) for a in amounts_tai]
    xiu_amts = [int(a) for a in amounts_xiu]
    all_amounts = sorted(
        {int(a) for a in tai_amts} | {int(a) for a in xiu_amts},
        reverse=True,
    )

    if strategy == 1 and all_amounts:
        strat_user: dict[str, Any] | None = None
        strat_amt: int | None = None

        if priority_users:
            # LC79: thử PRIORITY_USERS trước; không ai gánh được → coi như không có priority.
            prio_set = set(priority_users)
            prio_order = {u: i for i, u in enumerate(priority_users)}
            priority_pool = sorted(
                [r for r in pool if str(r.get("username") or r["id"]) in prio_set],
                key=lambda r: prio_order.get(str(r.get("username") or r["id"]), 9999),
            )
            strat_user, strat_amt = _find_largest_slot_user(
                priority_pool,
                daily,
                all_amounts,
                cap_vnd=cap_vnd,
                check_balance=check_balance,
                min_remainder_vnd=min_remainder_vnd,
            )

        if strat_user is None:
            strat_user, strat_amt = _find_largest_slot_user(
                _pool_sorted_by_daily_desc(pool, daily),
                daily,
                all_amounts,
                cap_vnd=cap_vnd,
                check_balance=check_balance,
                min_remainder_vnd=min_remainder_vnd,
            )

        if strat_user is None or strat_amt is None:
            if match_mode != 0:
                return (
                    [],
                    f"hủy phiên — không acc nào gánh được mức lớn "
                    f"(balance/cap; pool {len(pool)} acc, cap {cap_vnd:,})",
                    "",
                )
        else:
            strat_side = _pick_strat_side(int(strat_amt), tai_amts, xiu_amts)
            suid = str(strat_user["id"])
            used.add(suid)
            slots_out.append(
                BetSlot(
                    account_id=suid,
                    username=str(strat_user.get("username") or suid),
                    side=strat_side,
                    amount_vnd=int(strat_amt),
                )
            )
            if strat_side == "tai":
                tai_amts = _remove_one_amount(tai_amts, int(strat_amt))
            else:
                xiu_amts = _remove_one_amount(xiu_amts, int(strat_amt))

    partial_notes: list[str] = []
    tai_slots, err, tai_note = _assign_side_orders(
        pool,
        daily,
        tai_amts,
        "tai",
        used,
        strategy=strategy,
        cap_vnd=cap_vnd,
        check_balance=check_balance,
        min_remainder_vnd=min_remainder_vnd,
        match_mode=match_mode,
        priority_users=priority_users,
    )
    if err:
        return [], err, ""
    if tai_note:
        partial_notes.append(tai_note)
    xiu_slots, err, xiu_note = _assign_side_orders(
        pool,
        daily,
        xiu_amts,
        "xiu",
        used,
        strategy=strategy,
        cap_vnd=cap_vnd,
        check_balance=check_balance,
        min_remainder_vnd=min_remainder_vnd,
        match_mode=match_mode,
        priority_users=priority_users,
    )
    if err:
        return [], err, ""
    if xiu_note:
        partial_notes.append(xiu_note)
    all_slots = slots_out + tai_slots + xiu_slots
    if partial_notes:
        if all_slots:
            return all_slots, "", "; ".join(partial_notes)
        return (
            [],
            f"hủy phiên — {partial_notes[0]}",
            "",
        )
    return all_slots, "", ""


def print_assign_plan(
    slots: list[BetSlot],
    *,
    issue: str = "",
    game_key: str = "",
    game_label: str = "",
    jackpot_vnd: float = 0,
    game_id: int | None = None,
    cfg: dict | None = None,
) -> None:
    """In kế hoạch cược (format LC79)."""
    from xoso66_config_util import load_config
    from xoso66_bet_plan_log import build_planned_bets, log_lc79_bet_plan

    if cfg is None:
        cfg = load_config()
    if issue:
        print(f"issue={issue}", flush=True)
    log_lc79_bet_plan(build_planned_bets(slots, cfg))


def assign_session_bets(
    cfg: dict,
    *,
    jackpot_vnd: float | None = None,
    game_id: int | None = None,
) -> tuple[list[BetSlot], str, str]:
    """
    1) Chia mức Tài / Xỉu (random số acc, không cố định).
    2) Gán acc từ pool WS — kiểm tra balance + cap từng mức theo chiến lược.
    assign_match_mode=1: dư mức → hủy phiên. 0: trả slot đã gán + partial_note.
  Returns: (slots, err, partial_note)
    """
    acfg = _auto_bet_cfg(cfg)
    step = int(acfg.get("bet_step_vnd") or 10_000)
    max_per_user = _max_bet_per_user_vnd(acfg)
    strategy = int(acfg.get("assign_strategy") or 1)
    if strategy not in (1, 2, 3):
        strategy = 1
    cap_vnd = _daily_cap_vnd(acfg)

    pool, pool_err = _build_assign_pool(cfg, acfg)
    if pool_err or not pool:
        from xoso66_ws_pool import _maybe_auto_switch_assign_strategy_when_no_ws_tasks

        if strategy in (1, 2) and _maybe_auto_switch_assign_strategy_when_no_ws_tasks(cfg):
            from xoso66_config_util import load_config

            cfg = load_config()
            acfg = _auto_bet_cfg(cfg)
            strategy = int(acfg.get("assign_strategy") or 1)
            if strategy not in (1, 2, 3):
                strategy = 1
            pool, pool_err = _build_assign_pool(cfg, acfg)
        if pool_err:
            return [], pool_err, ""
        if not pool:
            return [], "hủy phiên — không có acc trong pool WS", ""

    daily = resolve_daily_bets(pool, acfg)
    if strategy != 3:
        pool = _evict_ws_if_daily_exhausted(cfg, pool, daily, acfg)
        if not pool:
            return [], "hủy phiên — không còn acc WS sau lọc cap cược ngày", ""

    if strategy == 3:
        slots, err, partial_note = _assign_strategy_3_pair(
            pool,
            daily,
            step=step,
            min_withdraw_vnd=consolidate_min_withdraw_vnd(acfg),
            max_gap_vnd=consolidate_pair_max_gap_vnd(acfg),
        )
        if err:
            return [], err, ""
        if not slots:
            return [], "hủy phiên — không gán được lệnh nào", ""
        pair_err = verify_pair_balanced(slots)
        if pair_err:
            return [], pair_err, ""
        return sort_slots_by_amount_desc(slots), "", partial_note

    side_total = resolve_side_total_vnd(cfg, jackpot_vnd, game_id=game_id)

    try:
        amounts_tai = split_side_total(
            side_total,
            step,
            max_per_user_vnd=max_per_user,
        )
        amounts_xiu = split_side_total(
            side_total,
            step,
            max_per_user_vnd=max_per_user,
        )
    except ValueError as e:
        return [], str(e), ""

    match_mode = assign_match_mode(acfg)

    check_bal = _check_balance_enabled(acfg)
    remainder = _assign_min_remainder_vnd(cfg, acfg) if check_bal else 0
    priority_users = _priority_users_from_cfg(cfg)
    slots, err, partial_note = _assign_by_strategy(
        pool,
        daily,
        amounts_tai,
        amounts_xiu,
        strategy=strategy,
        cap_vnd=cap_vnd,
        check_balance=check_bal,
        min_remainder_vnd=remainder,
        match_mode=match_mode,
        priority_users=priority_users if priority_users else None,
    )
    if err:
        return [], err, ""
    if not slots:
        return [], "hủy phiên — không gán được lệnh nào", ""
    if match_mode == 1:
        tot_err = verify_side_totals(slots, side_total)
        if tot_err:
            return [], tot_err, ""
    return sort_slots_by_amount_desc(slots), "", partial_note


def verify_side_totals(
    slots: list[BetSlot], side_total_vnd: int
) -> str | None:
    """None nếu mỗi bên đủ tổng; else thông báo lỗi."""
    target = int(side_total_vnd)
    tai = sum(s.amount_vnd for s in slots if s.side == "tai")
    xiu = sum(s.amount_vnd for s in slots if s.side == "xiu")
    if tai != target or xiu != target:
        return (
            f"Tài {tai:,} / Xỉu {xiu:,} VND — cần mỗi bên {target:,} VND "
            f"({len([s for s in slots if s.side == 'tai'])} acc Tài, "
            f"{len([s for s in slots if s.side == 'xiu'])} acc Xỉu)"
        )
    if not slots:
        return "không có lệnh cược"
    return None


def _main() -> int:
    import argparse

    from xoso66_accounts_db import init_db
    from xoso66_config_util import load_config

    ap = argparse.ArgumentParser(description="Test chia tiền / gán acc (không cược)")
    ap.add_argument("-n", "--times", type=int, default=1, help="số lần random chia")
    ap.add_argument(
        "-s",
        "--strategy",
        type=int,
        default=None,
        help="1 hoặc 2 (ghi đè config)",
    )
    args = ap.parse_args()

    init_db()
    cfg = load_config()
    acfg = _auto_bet_cfg(cfg)
    acfg.setdefault("check_balance", False)
    if args.strategy is not None:
        acfg["assign_strategy"] = int(args.strategy)
    cfg = dict(cfg)
    cfg["auto_bet"] = acfg

    strat = int(acfg.get("assign_strategy") or 1)
    if strat not in (1, 2, 3):
        strat = 1
    cap = _daily_cap_vnd(acfg)
    print(
        f"Chiến lược {strat} ({STRATEGY_LABELS.get(strat, '?')}) | "
        f"cap ngày {cap:,} | check_balance={_check_balance_enabled(acfg)}",
        flush=True,
    )
    for i in range(max(1, args.times)):
        if args.times > 1:
            print(f"\n--- Lần {i + 1} ---", flush=True)
        slots, err, partial_note = assign_session_bets(cfg)
        if err:
            print(f"Lỗi: {err}", flush=True)
            return 1
        if partial_note:
            print(f"Một phần: {partial_note}", flush=True)
        print_assign_plan(slots, issue=f"demo-{i + 1}")
        from xoso66_bet_daily import resolve_daily_bets
        from xoso66_accounts_db import list_accounts_by_status

        daily = resolve_daily_bets(
            list_accounts_by_status(str(acfg.get("account_status") or STATUS_DANG_CHOI)),
            acfg,
        )
        for s in sorted(slots, key=lambda x: -x.amount_vnd):
            d0 = daily.get(s.account_id, 0)
            print(
                f"  {s.username} {s.side} {s.amount_vnd:,} "
                f"(ngày DB {d0:,.0f}; +{s.amount_vnd:,} khi cược OK)",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
