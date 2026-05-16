import random
import asyncio
import time
import threading
import requests
import contextlib
from typing import List, Tuple, Dict, Optional
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

from chiaTien_Tho import distribute_for_devices
from constants import active_ws, load_config
from telegram_notifier import send_telegram
from weekly_bet_mode_scheduler import weekly_bet_mode_forces_strategy
from auto_withdraw_on_won_session import is_user_waiting_to_withdraw

API_BASE = "http://127.0.0.1:3000"  # server.js


# ================= Helpers cấu hình theo khung giờ =================
from time_windows import get_active_window as _get_active_window


def _clean(lst):
    # bỏ phần tử rỗng và strip khoảng trắng
    return [str(x).strip() for x in lst if isinstance(x, str) and str(x).strip()]


def _priority_users_from(cfg: dict, w: dict) -> List[str]:
    lst = w.get("PRIORITY_USERS") or cfg.get("PRIORITY_USERS") or []
    return [u for u in lst if u]

def _priority_users_v2_from(cfg: dict, w: dict) -> List[str]:
    lst = w.get("PRIORITY_USERS_V2") or cfg.get("PRIORITY_USERS_V2") or []
    return _clean(lst)
def _priority_users_v3_from(cfg: dict, w: dict) -> List[str]:
    lst = w.get("PRIORITY_USERS_V3") or cfg.get("PRIORITY_USERS_V3") or []
    return _clean(lst)


def _priority_users_min_bet_amount(cfg: dict, w: dict) -> int:
    """0 = không giới hạn. Ưu tiên TIME_WINDOWS rồi root config."""
    val = w.get("PRIORITY_USERS_MIN_BET_AMOUNT")
    if val is None:
        val = cfg.get("PRIORITY_USERS_MIN_BET_AMOUNT", 0)
    try:
        return max(0, int(val))
    except (TypeError, ValueError):
        return 0


def _iter_priority_users_for_bet(
    priority_users: List[str],
    amount: int,
    cfg: dict,
    w: dict,
):
    """PRIORITY_USERS chỉ được gán khi amount >= PRIORITY_USERS_MIN_BET_AMOUNT (nếu > 0)."""
    min_amt = _priority_users_min_bet_amount(cfg, w)
    if min_amt > 0 and amount < min_amt:
        return
    for u in priority_users:
        yield u


def _strategy9_plan_outside_max_amount(
    to_assign: List[Tuple[int, str]],
    online_users: List[str],
    balances: Dict[str, int],
    priority_users: List[str],
    config: dict,
    window: dict,
) -> Optional[int]:
    """
    Mô phỏng theo thứ tự mức cao→thấp: lệnh nào PRIORITY_USERS không lấy thì thuộc tầng ngoài V2/V3.
    Trả về mức cược lớn nhất trong các lệnh tầng đó (để gán 1 acc balance max).
    """
    used_sim: set[str] = set()
    outside_amounts: List[int] = []
    for amount, _door in to_assign:
        took_priority = False
        for u in _iter_priority_users_for_bet(priority_users, amount, config, window):
            if u in online_users and u not in used_sim:
                if balances.get(u, 0) >= amount:
                    used_sim.add(u)
                    took_priority = True
                    break
        if not took_priority:
            outside_amounts.append(amount)
    return max(outside_amounts) if outside_amounts else None


def _strategy9_outside_pool(
    candidates: List[Tuple[int, str, int]],
    priority_users: List[str],
    priority_v2: List[str],
    priority_v3: List[str],
) -> List[Tuple[int, str, int]]:
    """Tầng 2: ngoài V2/V3 và không thuộc PRIORITY_USERS."""
    prio_set = set(priority_users)
    return [
        t for t in candidates
        if t[1] not in priority_v2
        and t[1] not in priority_v3
        and t[1] not in prio_set
    ]


def _strategy9_choose_user(
    amount: int,
    online_users: List[str],
    used: set,
    candidates: List[Tuple[int, str, int]],
    balances: Dict[str, int],
    priority_users: List[str],
    priority_v2: List[str],
    priority_v3: List[str],
    config: dict,
    window: dict,
    outside_max_amount: Optional[int],
    outside_max_slot_used: bool,
) -> Tuple[Optional[str], Optional[int], Optional[int], bool]:
    """
    Chiến lược 9 only.
    Tầng 1: PRIORITY_USERS (nếu đủ điều kiện mức cược).
    Tầng 2: pool ngoài V2/V3 + không thuộc PRIORITY_USERS — mức lớn nhất trong tầng 2
    (đã tính đầu phiên) → 1 acc balance lớn nhất; các lệnh tầng 2 còn lại → random.
    """
    chosen: Optional[str] = None
    after: Optional[int] = None
    bal: Optional[int] = None

    for u in _iter_priority_users_for_bet(priority_users, amount, config, window):
        if u in online_users and u not in used:
            b = balances.get(u, 0)
            if b >= amount:
                chosen = u
                bal = b
                after = b - amount
                break
    if chosen is not None:
        print(
            f"[STRAT9] mức={amount} → PRIORITY_USERS: {chosen} (bal={bal})",
            flush=True,
        )
        return chosen, after, bal, outside_max_slot_used

    outside_pool = _strategy9_outside_pool(
        candidates, priority_users, priority_v2, priority_v3
    )
    pool9 = outside_pool if outside_pool else candidates
    use_max_slot = (
        outside_max_amount is not None
        and amount == outside_max_amount
        and not outside_max_slot_used
    )
    if use_max_slot:
        after, chosen, bal = max(pool9, key=lambda t: t[2])
        outside_max_slot_used = True
        reason = (
            f"mức={amount} → tầng 2: cược lớn nhất còn lại, balance max "
            f"(không PRIORITY/V2/V3): {chosen} (bal={bal})"
        )
    else:
        after, chosen, bal = random.choice(pool9)
        reason = f"mức={amount} → tầng 2: random (không PRIORITY/V2/V3): {chosen} (bal={bal})"
    if not outside_pool:
        reason += " [fallback: không có ai trong pool tầng 2]"
    print(f"[STRAT9] {reason}", flush=True)
    return chosen, after, bal, outside_max_slot_used


def _strategy8_sim_pick_v2v3(
    amount: int,
    used_sim: set[str],
    online_users: List[str],
    balances: Dict[str, int],
    priority_v2: List[str],
    priority_v3: List[str],
    today_bets: Dict[str, int],
) -> Optional[str]:
    """Mô phỏng nhánh V2 rồi V3 (today_bets thấp) — giống gán thật strategy 8."""
    for u in sorted(
        [x for x in priority_v2 if x in online_users and x not in used_sim],
        key=lambda x: (today_bets.get(x, 0), balances.get(x, 0)),
    ):
        if balances.get(u, 0) >= amount:
            return u
    for u in sorted(
        [x for x in priority_v3 if x in online_users and x not in used_sim],
        key=lambda x: (today_bets.get(x, 0), balances.get(x, 0)),
    ):
        if balances.get(u, 0) >= amount:
            return u
    return None


def _strategy8_plan_outside_session(
    to_assign: List[Tuple[int, str]],
    online_users: List[str],
    balances: Dict[str, int],
    priority_users: List[str],
    priority_v2: List[str],
    priority_v3: List[str],
    today_bets: Dict[str, int],
    config: dict,
    window: dict,
) -> Tuple[Optional[int], Optional[str], List[str]]:
    """
    Mô phỏng phiên strategy 8: lệnh nào rơi vào nhánh others (ngoài V2/V3).
    Trả (mức max others, user balance max, head_pair).
    """
    used_sim: set[str] = set()
    others_amounts: List[int] = []
    for amount, _door in to_assign:
        took_priority = False
        for u in _iter_priority_users_for_bet(priority_users, amount, config, window):
            if u in online_users and u not in used_sim:
                if balances.get(u, 0) >= amount:
                    used_sim.add(u)
                    took_priority = True
                    break
        if took_priority:
            continue
        u_v = _strategy8_sim_pick_v2v3(
            amount, used_sim, online_users, balances, priority_v2, priority_v3, today_bets
        )
        if u_v:
            used_sim.add(u_v)
            continue
        others_amounts.append(amount)

    outside_max = max(others_amounts) if others_amounts else None

    pool = [u for u in online_users if u not in priority_v2 and u not in priority_v3]
    user_max_bal: Optional[str] = None
    if pool:
        user_max_bal = max(pool, key=lambda u: (balances.get(u, 0), u))

    head_pair: List[str] = []
    sim_rows: List[Tuple[str, int, int]] = []
    if others_amounts:
        sim_amounts = list(others_amounts)
        if outside_max is not None:
            try:
                sim_amounts.remove(outside_max)
            except ValueError:
                pass
        if sim_amounts:
            sim_rows = _strategy10_simulate_tight_fit_allocations(
                online_users,
                sorted(sim_amounts, reverse=True),
                balances,
                priority_v2,
                priority_v3,
            )
            head_pair = _strategy10_priority_two_from_simulation(sim_rows)

    return outside_max, user_max_bal, head_pair


def _strategy8_outside_try_order(
    online_users: List[str],
    used: set,
    amount: int,
    balances: Dict[str, int],
    priority_v2: List[str],
    priority_v3: List[str],
    head_pair: List[str],
) -> List[str]:
    """Nhánh others strategy 8: thử head_pair (mô phỏng giống strat 10) rồi random phần còn lại."""
    others = [
        u for u in online_users
        if u not in priority_v2
        and u not in priority_v3
        and u not in used
    ]
    others_set = set(others)
    head: List[str] = []
    for u in head_pair:
        if u in others_set and balances.get(u, 0) >= amount:
            head.append(u)
    eligible_rest = [
        u for u in others
        if u not in head and balances.get(u, 0) >= amount
    ]
    random.shuffle(eligible_rest)
    return head + eligible_rest


def _strategy8_choose_from_outside(
    amount: int,
    online_users: List[str],
    used: set,
    balances: Dict[str, int],
    candidates: List[Tuple[int, str, int]],
    priority_v2: List[str],
    priority_v3: List[str],
    outside_max_amount: Optional[int],
    user_max_bal: Optional[str],
    max_bal_slot_used: bool,
    head_pair: List[str],
) -> Tuple[Optional[str], Optional[int], Optional[int], bool]:
    """Chọn user nhánh others (ngoài V2/V3) — strategy 8 only."""
    outside_pool = [
        t for t in candidates
        if t[1] not in priority_v2 and t[1] not in priority_v3
    ]
    if not outside_pool:
        return None, None, None, max_bal_slot_used

    if (
        outside_max_amount is not None
        and amount == outside_max_amount
        and not max_bal_slot_used
        and user_max_bal
        and user_max_bal not in used
    ):
        for after_c, u, bal_c in outside_pool:
            if u == user_max_bal:
                max_bal_slot_used = True
                return u, after_c, bal_c, max_bal_slot_used

    by_user = {u: (after_c, bal_c) for after_c, u, bal_c in outside_pool}
    for u in _strategy8_outside_try_order(
        online_users,
        used,
        amount,
        balances,
        priority_v2,
        priority_v3,
        head_pair,
    ):
        if u in by_user:
            after_c, bal_c = by_user[u]
            return u, after_c, bal_c, max_bal_slot_used

    after_c, u, bal_c = random.choice(outside_pool)
    return u, after_c, bal_c, max_bal_slot_used


def _strategy7_target_sum(cfg: dict, window: dict) -> int:
    """Ngưỡng xxx: balance + mức cược so với giá trị này. Ưu tiên TIME_WINDOWS."""
    val = window.get("STRATEGY_7_TARGET_SUM")
    if val is None:
        block = cfg.get("STRATEGY_7") or {}
        val = block.get("TARGET_SUM")
    if val is None:
        val = cfg.get("STRATEGY_7_TARGET_SUM", 310000)
    try:
        return int(val)
    except (TypeError, ValueError):
        return 310000


def _strategy7_pick_priority_user(
    online_users: List[str],
    balances: Dict[str, int],
    max_bet_amount: int,
    target_sum: int,
) -> Optional[str]:
    """
    Chọn 1 acc: balance + mức cược lớn nhất phiên gần target_sum nhất;
    hòa thì ưu tiên tổng >= target_sum. Chỉ xét user đủ tiền cho mức max.
    """
    best_u: Optional[str] = None
    best_key: Optional[Tuple[int, int, str]] = None
    for u in online_users:
        bal = balances.get(u, 0)
        if bal < max_bet_amount:
            continue
        total = bal + max_bet_amount
        dist = abs(total - target_sum)
        key = (dist, -total, u)
        if best_key is None or key < best_key:
            best_key = key
            best_u = u
    return best_u


def _strategy7_pick_min_amount_user(
    online_users: List[str],
    balances: Dict[str, int],
    min_bet_amount: int,
    exclude: Optional[str] = None,
) -> Optional[str]:
    """Mức nhỏ nhất: acc đủ tiền có balance gần mức min nhất (|balance − min| nhỏ nhất)."""
    best_u: Optional[str] = None
    best_key: Optional[Tuple[int, str]] = None
    for u in online_users:
        if exclude and u == exclude:
            continue
        bal = balances.get(u, 0)
        if bal < min_bet_amount:
            continue
        dist = abs(bal - min_bet_amount)
        key = (dist, u)
        if best_key is None or key < best_key:
            best_key = key
            best_u = u
    return best_u


def _strategy_from(cfg: dict, w: dict, fallback: int = 1) -> int:
    """
    Ưu tiên ASSIGN_STRATEGY trong window nếu là số hợp lệ (1..12).
    Nếu không có/không hợp lệ => dùng root; nếu root không hợp lệ => fallback.
    """
    win_val = w.get("ASSIGN_STRATEGY")
    if isinstance(win_val, int) and 1 <= win_val <= 12:
        return win_val
    try:
        root_val = int(cfg.get("ASSIGN_STRATEGY", fallback))
        if 1 <= root_val <= 12:
            return root_val
    except Exception:
        pass
    return fallback


# ================= Helpers khác =================

def _delayed_het_tien_check(user: str) -> None:
    """Sau 20s check lại số dư, nếu vẫn < 10k thì ép Hết Tiền rồi quyết định nạp (không chờ thêm 20s)."""
    from streak_deposit_scheduler import het_tien_slot_try_acquire, het_tien_slot_release

    if not het_tien_slot_try_acquire(user):
        return
    try:
        time.sleep(20)
        try:
            r = requests.get(f"{API_BASE}/api/users/{user}", timeout=5)
            if r.status_code == 200:
                balance = int(r.json().get("balance") or 0)
                if balance < 10000:
                    with contextlib.suppress(Exception):
                        requests.put(f"{API_BASE}/api/users/{user}", json={"status": "Hết Tiền"})
                    try:
                        from streak_deposit_scheduler import run_het_tien_deposit_decision
                        run_het_tien_deposit_decision(user)
                    except Exception as e:
                        print(f"[ERROR] run_het_tien_deposit_decision({user}): {e}")
        except Exception as e:
            print(f"⚠️ delayed_het_tien_check {user}: {e}")
    finally:
        het_tien_slot_release(user)


def _fresh_balances_for_online(online_users: List[str]) -> Dict[str, int]:
    balances = {}
    for user in online_users:
        try:
            r = requests.get(f"{API_BASE}/api/users/{user}", timeout=5)
            if r.status_code == 200:
                data = r.json()
                balance = int(data.get("balance") or 0)
                balances[user] = balance

                if balance < 10000:
                    # Chờ 20s rồi check lại, chỉ ép Hết Tiền nếu vẫn < 10k
                    threading.Thread(target=_delayed_het_tien_check, args=(user,), daemon=True).start()
                # else:
                #     with contextlib.suppress(Exception):
                #         requests.put(f"{API_BASE}/api/users/{user}", json={"status": "Đang Chơi"})
            else:
                balances[user] = 0
                with contextlib.suppress(Exception):
                    requests.put(f"{API_BASE}/api/users/{user}", json={"status": "Hết Tiền"})
                try:
                    from streak_deposit_scheduler import schedule_het_tien_deposit_after_delay
                    schedule_het_tien_deposit_after_delay(user)
                except Exception as ex:
                    print(f"[ERROR] schedule_het_tien_deposit_after_delay({user}): {ex}")
        except Exception as e:
            print(f"⚠️ Lỗi lấy balance cho {user}: {e}")
            balances[user] = 0
            with contextlib.suppress(Exception):
                requests.put(f"{API_BASE}/api/users/{user}", json={"status": "Hết Tiền"})
            try:
                from streak_deposit_scheduler import schedule_het_tien_deposit_after_delay
                schedule_het_tien_deposit_after_delay(user)
            except Exception as ex:
                print(f"[ERROR] schedule_het_tien_deposit_after_delay({user}): {ex}")
    return balances


def _fetch_today_bets_for_online(online_users: List[str]) -> Dict[str, int]:
    """
    Lấy tổng cược ngày cho các user online từ API /api/bet-totals.
    Kết quả: {username: total_bet_today}
    """
    res: Dict[str, int] = {u: 0 for u in online_users}
    try:
        r = requests.get(f"{API_BASE}/api/bet-totals", params={"page": 1, "limit": 10000}, timeout=6)
        if r.status_code != 200:
            return res
        data = r.json()
        items = data.get("data") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return res
        for item in items:
            try:
                u = str(item.get("username") or item.get("user") or "").strip()
                if u and u in res:
                    total_val = (item.get("total_day")
                                 or item.get("totalBet")
                                 or item.get("total")
                                 or item.get("today_bet")
                                 or item.get("todayBet") or 0)
                    res[u] = int(total_val or 0)
            except Exception:
                continue
    except Exception:
        return res
    return res


def _fetch_weekly_bets_for_online(online_users: List[str]) -> Dict[str, int]:
    """
    Lấy tổng cược tuần cho các user online từ API /api/bet-totals.
    Kết quả: {username: total_bet_week}
    """
    res: Dict[str, int] = {u: 0 for u in online_users}
    try:
        r = requests.get(f"{API_BASE}/api/bet-totals", params={"page": 1, "limit": 10000}, timeout=6)
        if r.status_code != 200:
            return res
        data = r.json()
        items = data.get("data") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return res
        for item in items:
            try:
                u = str(item.get("username") or item.get("user") or "").strip()
                if u and u in res:
                    total_val = (item.get("total_week")
                                 or item.get("totalWeek")
                                 or item.get("week_bet")
                                 or item.get("weekBet") or 0)
                    res[u] = int(total_val or 0)
            except Exception:
                continue
    except Exception:
        return res
    return res


def _fetch_monthly_bets_for_online(online_users: List[str]) -> Dict[str, int]:
    """
    Lấy tổng cược tháng cho các user online từ API /api/bet-totals.
    Kết quả: {username: total_bet_month}
    """
    res: Dict[str, int] = {u: 0 for u in online_users}
    try:
        r = requests.get(f"{API_BASE}/api/bet-totals", params={"page": 1, "limit": 10000}, timeout=6)
        if r.status_code != 200:
            return res
        data = r.json()
        items = data.get("data") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return res
        for item in items:
            try:
                u = str(item.get("username") or item.get("user") or "").strip()
                if u and u in res:
                    total_val = (item.get("total_month")
                                 or item.get("totalMonth")
                                 or item.get("month_bet")
                                 or item.get("monthBet") or 0)
                    res[u] = int(total_val or 0)
            except Exception:
                continue
    except Exception:
        return res
    return res

# ================= Gán cược =================


def _strategy10_simulate_tight_fit_allocations(
    online_users: List[str],
    amounts_high_to_low: List[int],
    balances: Dict[str, int],
    priority_v2: List[str],
    priority_v3: List[str],
) -> List[Tuple[str, int, int]]:
    """
    Mức cược từ **cao → thấp** (đúng thứ tự `to_assign`): mỗi mức gán 1 user ngoài V2/V3
    (chưa dùng trong mô phỏng) có **balance nhỏ nhất** mà vẫn >= mức — tức “trên mức
    gần nhất” / khớp chặt. Trả [(user, amount, balance_sau_cược), ...].
    """
    used_sim: set[str] = set()
    out: List[Tuple[str, int, int]] = []
    for amt in amounts_high_to_low:
        pool = [
            u for u in online_users
            if u not in used_sim
            and u not in priority_v2
            and u not in priority_v3
            and balances.get(u, 0) >= amt
        ]
        if not pool:
            break
        u = min(pool, key=lambda x: (balances.get(x, 0), x))
        b = balances.get(u, 0)
        out.append((u, amt, b - amt))
        used_sim.add(u)
    return out


def _strategy10_priority_two_from_simulation(
    sim_rows: List[Tuple[str, int, int]],
) -> List[str]:
    """
    Sau mô phỏng: lấy **tối đa 2 user** có **balance sau cược** (`after`) nhỏ nhất
    (tăng dần theo after; hòa thì username).
    """
    if not sim_rows:
        return []
    by_after = sorted(sim_rows, key=lambda t: (t[2], t[0]))
    picked: List[str] = []
    for u, _amt, _after in by_after:
        if u not in picked:
            picked.append(u)
        if len(picked) >= 2:
            break
    return picked


def _strategy10_user_try_order(
    online_users: List[str],
    used: set,
    amount: int,
    balances: Dict[str, int],
    priority_v2: List[str],
    priority_v3: List[str],
    head_pair: List[str],
) -> List[str]:
    """
    Chiến lược 10 — nhánh ngoài V2/V3 (sau PRIORITY_USERS): thử trước `head_pair` (tối đa 2
    acc do mô phỏng tight-fit toàn phiên chọn); sau đó các acc ngoài V2/V3 còn lại đủ tiền
    + V2 + V3: random.
    """
    others = [
        u for u in online_users
        if u not in priority_v2
        and u not in priority_v3
        and u not in used
    ]
    others_set = set(others)
    head: List[str] = []
    for u in head_pair:
        if u in others_set and balances.get(u, 0) >= amount:
            head.append(u)

    eligible_rest = [
        u for u in others
        if u not in head and balances.get(u, 0) >= amount
    ]

    v2_ok = [
        u for u in priority_v2
        if u in online_users and u not in used and balances.get(u, 0) >= amount
    ]
    v3_ok = [
        u for u in priority_v3
        if u in online_users and u not in used and balances.get(u, 0) >= amount
    ]
    tail = eligible_rest + v2_ok + v3_ok
    random.shuffle(tail)
    return head + tail


def assign_bets(
    bets: List[Tuple[None, int, str]],
    online_users: List[str],
    strategy: int = None
) -> List[Tuple[str, int, str, int]]:
    """
    Trả về list (username, amount, door, delay)
    """
    config = load_config()
    window = _get_active_window(config)
    try:
        strategy = int(strategy) if strategy is not None else 1
    except (TypeError, ValueError):
        strategy = 1

    # PAUSE theo khung giờ
    if window.get("PAUSE"):
        msg = "⏸️ PAUSE theo khung giờ: bỏ qua phiên gán cược."
        print(msg)
        return []

    try:
        from jackpot_night_extend import (
            format_jackpot_gate_skip_reason,
            jackpot_periodic_gate_allows_betting,
        )
    except ImportError:
        jackpot_periodic_gate_allows_betting = None  # type: ignore
        format_jackpot_gate_skip_reason = None  # type: ignore
    if jackpot_periodic_gate_allows_betting and not jackpot_periodic_gate_allows_betting(config):
        if format_jackpot_gate_skip_reason:
            print(format_jackpot_gate_skip_reason(config, action="bỏ qua gán cược"), flush=True)
        else:
            print(
                "⏸️ Jackpot: chưa trên JACKPOT_THRESHOLD hoặc đã dừng sau nổ hũ → bỏ qua gán cược.",
                flush=True,
            )
        return []

    # Lấy PRIORITY_USERS/ASSIGN_STRATEGY theo giờ
    PRIORITY_USERS = _priority_users_from(config, window)  # vẫn dùng cho các strategy khác
    PRIORITY_USERS_V2 = _priority_users_v2_from(config, window)
    PRIORITY_USERS_V3 = _priority_users_v3_from(config, window)

    balances = _fresh_balances_for_online(online_users)
    today_bets = _fetch_today_bets_for_online(online_users) if (
        strategy in (8, 10, 11, 12)
    ) else {}
    weekly_bets = _fetch_weekly_bets_for_online(online_users) if strategy in (6, 8) else {}
    monthly_bets = _fetch_monthly_bets_for_online(online_users) if strategy == 5 else {}

    # sort giảm dần theo amount để nhận diện bet lớn nhất
    to_assign = sorted([(amt, door) for (_dev, amt, door) in bets], key=lambda x: -x[0])

    # Chiến lược 10: mô phỏng gán tight-fit theo mức cao→thấp, rồi lấy 2 user có dư sau cược
    # nhỏ nhất trong mô phỏng làm head_pair (chỉ nhánh ngoài V2/V3; PRIORITY_USERS giữ nguyên).
    strategy10_head_pair: List[str] = []
    if strategy == 10 and to_assign:
        amounts_desc = [amt for amt, _door in to_assign]
        sim10 = _strategy10_simulate_tight_fit_allocations(
            online_users,
            amounts_desc,
            balances,
            PRIORITY_USERS_V2,
            PRIORITY_USERS_V3,
        )
        strategy10_head_pair = _strategy10_priority_two_from_simulation(sim10)
        sim_line = (
            "; ".join(f"{u} mức{m}→dư{r}" for u, m, r in sim10) if sim10 else "(mô phỏng rỗng)"
        )
        pair_line = ", ".join(strategy10_head_pair) if strategy10_head_pair else "(không đủ 2 user)"
        print(f"[STRAT10] Chuỗi mô phỏng (cao→thấp): {sim_line}", flush=True)
        print(f"[STRAT10] 2 acc ưu tiên (dư nhỏ nhất trong mô phỏng): {pair_line}", flush=True)

    used = set()
    odd_applied_v2: set = set()  # V2: đã áp dụng đánh lẻ 1 lần/phiên để tổng cược ngày khác bội 10k
    # State chỉ dùng trong elif strategy == 9 (không ảnh hưởng strategy 1–8, 10–12)
    strategy9_outside_max_amount: Optional[int] = None
    strategy9_outside_max_slot_used = False
    if strategy == 9 and to_assign:
        strategy9_outside_max_amount = _strategy9_plan_outside_max_amount(
            to_assign,
            online_users,
            balances,
            PRIORITY_USERS,
            config,
            window,
        )
        if strategy9_outside_max_amount is not None:
            print(
                f"[STRAT9] Tầng 2: mức lớn nhất sau PRIORITY = {strategy9_outside_max_amount} "
                f"→ 1 acc balance max (pool không gồm PRIORITY_USERS/V2/V3)",
                flush=True,
            )
    # State chỉ strategy 7
    strategy7_max_user: Optional[str] = None
    strategy7_max_amt: Optional[int] = None
    strategy7_max_slot_used = False
    strategy7_min_user: Optional[str] = None
    strategy7_min_amt: Optional[int] = None
    strategy7_min_slot_used = False
    if strategy == 7 and to_assign:
        strategy7_max_amt = max(amt for amt, _door in to_assign)
        strategy7_min_amt = min(amt for amt, _door in to_assign)
        target_sum = _strategy7_target_sum(config, window)
        strategy7_max_user = _strategy7_pick_priority_user(
            online_users, balances, strategy7_max_amt, target_sum
        )
        strategy7_min_user = _strategy7_pick_min_amount_user(
            online_users, balances, strategy7_min_amt, exclude=strategy7_max_user
        )
        if strategy7_min_user is None:
            strategy7_min_user = _strategy7_pick_min_amount_user(
                online_users, balances, strategy7_min_amt, exclude=None
            )
        if strategy7_max_user:
            bal_p = balances.get(strategy7_max_user, 0)
            total_p = bal_p + strategy7_max_amt
            print(
                f"[STRAT7] TARGET_SUM={target_sum}, mức max={strategy7_max_amt}, "
                f"user ưu tiên={strategy7_max_user} "
                f"(balance={bal_p}+max={total_p}, |Δ|={abs(total_p - target_sum)})",
                flush=True,
            )
        else:
            print(
                f"[STRAT7] TARGET_SUM={target_sum}, mức max={strategy7_max_amt}: "
                "không chọn được user ưu tiên max",
                flush=True,
            )
        if strategy7_min_amt != strategy7_max_amt and strategy7_min_user:
            bal_m = balances.get(strategy7_min_user, 0)
            print(
                f"[STRAT7] mức min={strategy7_min_amt}, user ưu tiên={strategy7_min_user} "
                f"(balance={bal_m}, |balance−min|={abs(bal_m - strategy7_min_amt)})",
                flush=True,
            )
        elif strategy7_min_amt == strategy7_max_amt:
            print("[STRAT7] mức min=max → chỉ 1 slot ưu tiên max", flush=True)

    # State chỉ strategy 8
    strategy8_outside_max_amount: Optional[int] = None
    strategy8_user_max_bal: Optional[str] = None
    strategy8_head_pair: List[str] = []
    strategy8_max_bal_slot_used = False
    if strategy == 8 and to_assign:
        (
            strategy8_outside_max_amount,
            strategy8_user_max_bal,
            strategy8_head_pair,
        ) = _strategy8_plan_outside_session(
            to_assign,
            online_users,
            balances,
            PRIORITY_USERS,
            PRIORITY_USERS_V2,
            PRIORITY_USERS_V3,
            today_bets,
            config,
            window,
        )
    final: List[Tuple[str, int, str, int]] = []

    # ---------------------------- VÒNG GÁN ----------------------------
    for idx, (amount, door) in enumerate(to_assign):
        chosen: Optional[str] = None
        after: Optional[int] = None
        _bal: Optional[int] = None
        candidates: List[Tuple[int, str, int]] = []

        # ứng viên cho mức amount ở lượt này
        for u in online_users:
            if u in used:
                continue
            bal = balances.get(u, 0)
            if bal >= amount:
                candidates.append((bal - amount, u, bal))  # (after, username, bal)

        if not candidates:
            msg = f"⚠️ Không tìm được user đủ tiền cho {door} {amount}. Hủy phiên."
            print(msg)
            send_telegram(msg)
            return []

        if chosen is None:
            # -------------------- Chiến lược chọn account --------------------
            if strategy == 1:
                # Ưu tiên PRIORITY_USERS, fallback AFTER thấp nhất
                chosen, after, _bal = None, None, None
                for u in _iter_priority_users_for_bet(PRIORITY_USERS, amount, config, window):
                    if u in online_users and u not in used:
                        bal = balances.get(u, 0)
                        if bal >= amount:
                            chosen = u
                            _bal = bal
                            after = bal - amount
                            break
                if chosen is None:
                    after, chosen, _bal = min(candidates, key=lambda t: t[0])  # AFTER thấp nhất
    
            elif strategy == 2:
                # Ưu tiên PRIORITY_USERS, fallback Random
                chosen, after, _bal = None, None, None
                for u in _iter_priority_users_for_bet(PRIORITY_USERS, amount, config, window):
                    if u in online_users and u not in used:
                        bal = balances.get(u, 0)
                        if bal >= amount:
                            chosen = u
                            _bal = bal
                            after = bal - amount
                            break
                if chosen is None:
                    after, chosen, _bal = random.choice(candidates)  # Random
    
            elif strategy == 3:
                # Ưu tiên PRIORITY_USERS, fallback AFTER thấp nhất
                chosen, after, _bal = None, None, None
                for u in _iter_priority_users_for_bet(PRIORITY_USERS, amount, config, window):
                    if u in online_users and u not in used:
                        bal = balances.get(u, 0)
                        if bal >= amount:
                            chosen = u
                            _bal = bal
                            after = bal - amount
                            break
                if chosen is None:
                    after, chosen, _bal = min(candidates, key=lambda t: t[0])
                if chosen is None:
                    msg = f"⚠️ Không tìm được user đủ tiền cho {door} {amount}. Hủy phiên."
                    print(msg)
                    send_telegram(msg)
                    return []
    
            elif strategy == 4:
                # Ưu tiên PRIORITY_USERS, fallback: balance cao → thấp cho users KHÔNG thuộc V2/V3, sau đó mới đến V2 rồi V3
                chosen, after, _bal = None, None, None
                for u in _iter_priority_users_for_bet(PRIORITY_USERS, amount, config, window):
                    if u in online_users and u not in used:
                        bal = balances.get(u, 0)
                        if bal >= amount:
                            chosen = u
                            _bal = bal
                            after = bal - amount
                            break
                if chosen is None:
                    others = [u for u in online_users if u not in used and u not in PRIORITY_USERS_V2 and u not in PRIORITY_USERS_V3]
                    others_sorted = sorted(others, key=lambda u: -balances.get(u, 0))
                    v2_sorted = sorted([u for u in PRIORITY_USERS_V2 if u in online_users and u not in used], key=lambda u: -balances.get(u, 0))
                    v3_sorted = sorted([u for u in PRIORITY_USERS_V3 if u in online_users and u not in used], key=lambda u: -balances.get(u, 0))
                    ordered = others_sorted + v2_sorted + v3_sorted
                    for u in ordered:
                        bal = balances.get(u, 0)
                        if bal >= amount:
                            chosen = u
                            _bal = bal
                            after = bal - amount
                            break
    
                if chosen is None:
                    msg = f"⚠️ Không tìm được user đủ tiền cho {door} {amount}. Hủy phiên."
                    print(msg)
                    send_telegram(msg)
                    return []
    
            elif strategy == 5:
                # Ưu tiên PRIORITY_USERS, fallback tổng cược tháng thấp nhất
                chosen, after, _bal = None, None, None
                for u in _iter_priority_users_for_bet(PRIORITY_USERS, amount, config, window):
                    if u in online_users and u not in used:
                        bal = balances.get(u, 0)
                        if bal >= amount:
                            chosen = u
                            _bal = bal
                            after = bal - amount
                            break
                if chosen is None:
                    candidates_sorted = sorted(candidates, key=lambda t: (monthly_bets.get(t[1], 0), t[2]))
                    after, chosen, _bal = candidates_sorted[0]
                if chosen is None:
                    msg = f"⚠️ Không tìm được user đủ tiền cho {door} {amount}. Hủy phiên."
                    print(msg)
                    send_telegram(msg)
                    return []
    
            elif strategy == 6:
                # Ưu tiên PRIORITY_USERS, fallback tổng cược tuần thấp nhất
                chosen, after, _bal = None, None, None
                for u in _iter_priority_users_for_bet(PRIORITY_USERS, amount, config, window):
                    if u in online_users and u not in used:
                        bal = balances.get(u, 0)
                        if bal >= amount:
                            chosen = u
                            _bal = bal
                            after = bal - amount
                            break
                if chosen is None:
                    candidates_sorted = sorted(candidates, key=lambda t: (weekly_bets.get(t[1], 0), t[2]))
                    after, chosen, _bal = candidates_sorted[0]
                if chosen is None:
                    msg = f"⚠️ Không tìm được user đủ tiền cho {door} {amount}. Hủy phiên."
                    print(msg)
                    send_telegram(msg)
                    return []
    
            elif strategy == 7:
                # Ưu tiên mức max (balance+max≈TARGET_SUM) + mức min (balance≈min); còn lại random
                chosen, after, _bal = None, None, None
                if (
                    not strategy7_max_slot_used
                    and strategy7_max_user
                    and strategy7_max_amt is not None
                    and amount == strategy7_max_amt
                    and strategy7_max_user in online_users
                    and strategy7_max_user not in used
                ):
                    bal = balances.get(strategy7_max_user, 0)
                    if bal >= amount:
                        chosen = strategy7_max_user
                        _bal = bal
                        after = bal - amount
                        strategy7_max_slot_used = True
                if (
                    chosen is None
                    and not strategy7_min_slot_used
                    and strategy7_min_user
                    and strategy7_min_amt is not None
                    and amount == strategy7_min_amt
                    and strategy7_min_amt != strategy7_max_amt
                    and strategy7_min_user in online_users
                    and strategy7_min_user not in used
                ):
                    bal = balances.get(strategy7_min_user, 0)
                    if bal >= amount:
                        chosen = strategy7_min_user
                        _bal = bal
                        after = bal - amount
                        strategy7_min_slot_used = True
                if chosen is None:
                    pool = [
                        u for u in online_users
                        if u not in used and balances.get(u, 0) >= amount
                    ]
                    if pool:
                        chosen = random.choice(pool)
                        _bal = balances.get(chosen, 0)
                        after = _bal - amount

                if chosen is None:
                    msg = f"⚠️ Không tìm được user đủ tiền cho {door} {amount}. Hủy phiên."
                    print(msg)
                    send_telegram(msg)
                    return []
    
            elif strategy == 8:
                # PRIORITY_USERS → V2/V3 (today thấp) → others: balance max (9) + head_pair (10) → random
                chosen, after, _bal = None, None, None
                for u in _iter_priority_users_for_bet(PRIORITY_USERS, amount, config, window):
                    if u in online_users and u not in used:
                        bal = balances.get(u, 0)
                        if bal >= amount:
                            chosen = u
                            _bal = bal
                            after = bal - amount
                            break
                if chosen is None:
                    u_v = _strategy8_sim_pick_v2v3(
                        amount,
                        used,
                        online_users,
                        balances,
                        PRIORITY_USERS_V2,
                        PRIORITY_USERS_V3,
                        today_bets,
                    )
                    if u_v:
                        chosen = u_v
                        _bal = balances.get(chosen, 0)
                        after = _bal - amount
                if chosen is None:
                    chosen, after, _bal, strategy8_max_bal_slot_used = (
                        _strategy8_choose_from_outside(
                            amount,
                            online_users,
                            used,
                            balances,
                            candidates,
                            PRIORITY_USERS_V2,
                            PRIORITY_USERS_V3,
                            strategy8_outside_max_amount,
                            strategy8_user_max_bal,
                            strategy8_max_bal_slot_used,
                            strategy8_head_pair,
                        )
                    )

                if chosen is None:
                    msg = f"⚠️ Không tìm được user đủ tiền cho {door} {amount}. Hủy phiên."
                    print(msg)
                    send_telegram(msg)
                    return []
    
            elif strategy == 9:
                chosen, after, _bal, strategy9_outside_max_slot_used = _strategy9_choose_user(
                    amount,
                    online_users,
                    used,
                    candidates,
                    balances,
                    PRIORITY_USERS,
                    PRIORITY_USERS_V2,
                    PRIORITY_USERS_V3,
                    config,
                    window,
                    strategy9_outside_max_amount,
                    strategy9_outside_max_slot_used,
                )

            elif strategy == 10:
                chosen, after, _bal = None, None, None
                for u in _iter_priority_users_for_bet(PRIORITY_USERS, amount, config, window):
                    if u in online_users and u not in used:
                        bal = balances.get(u, 0)
                        if bal >= amount:
                            chosen = u
                            _bal = bal
                            after = bal - amount
                            break
                if chosen is None:
                    ordered = _strategy10_user_try_order(
                        online_users,
                        used,
                        amount,
                        balances,
                        PRIORITY_USERS_V2,
                        PRIORITY_USERS_V3,
                        strategy10_head_pair,
                    )
                    for u in ordered:
                        bal = balances.get(u, 0)
                        if bal >= amount:
                            chosen = u
                            _bal = bal
                            after = bal - amount
                            break

                if chosen is None:
                    msg = f"⚠️ Không tìm được user đủ tiền cho {door} {amount}. Hủy phiên."
                    print(msg)
                    send_telegram(msg)
                    return []
            elif strategy == 11:
                # Ưu tiên PRIORITY_USERS, fallback: 1️⃣ User KHÔNG thuộc V2 & V3 → balance tăng dần
                chosen, after, _bal = None, None, None
                for u in _iter_priority_users_for_bet(PRIORITY_USERS, amount, config, window):
                    if u in online_users and u not in used:
                        bal = balances.get(u, 0)
                        if bal >= amount:
                            chosen = u
                            _bal = bal
                            after = bal - amount
                            break
                if chosen is None:
                    others = [
                        u for u in online_users
                        if u not in PRIORITY_USERS_V2
                        and u not in PRIORITY_USERS_V3
                        and u not in used
                    ]
                    others_sorted = sorted(others, key=lambda u: balances.get(u, 0))
                    v2_sorted = sorted(
                        [u for u in PRIORITY_USERS_V2 if u in online_users and u not in used],
                        key=lambda u: (today_bets.get(u, 0), balances.get(u, 0))
                    )
                    v3_sorted = sorted(
                        [u for u in PRIORITY_USERS_V3 if u in online_users and u not in used],
                        key=lambda u: (today_bets.get(u, 0), balances.get(u, 0))
                    )
                    ordered = others_sorted + v2_sorted + v3_sorted
                    for u in ordered:
                        bal = balances.get(u, 0)
                        if bal >= amount:
                            chosen = u
                            _bal = bal
                            after = bal - amount
                            break
    
                if chosen is None:
                    msg = f"⚠️ Không tìm được user đủ tiền cho {door} {amount}. Hủy phiên."
                    print(msg)
                    send_telegram(msg)
                    return []
            elif strategy == 12:
                # Ưu tiên PRIORITY_USERS (đủ tiền, online, chưa dùng); sau đó mọi acc còn lại theo today_bets thấp nhất.
                chosen, after, _bal = None, None, None
                for u in _iter_priority_users_for_bet(PRIORITY_USERS, amount, config, window):
                    if u in online_users and u not in used:
                        bal = balances.get(u, 0)
                        if bal >= amount:
                            chosen = u
                            _bal = bal
                            after = bal - amount
                            break
                if chosen is None:
                    eligible = [u for u in online_users if u not in used and balances.get(u, 0) >= amount]
                    if eligible:
                        chosen = min(eligible, key=lambda u: today_bets.get(u, 0))
                        _bal = balances[chosen]
                        after = _bal - amount
    
                if chosen is None:
                    msg = f"⚠️ Không tìm được user đủ tiền cho {door} {amount}. Hủy phiên."
                    print(msg)
                    send_telegram(msg)
                    return []
    
            else:
                # fallback an toàn
                after, chosen, _bal = random.choice(candidates)

        if chosen is None:
            msg = f"⚠️ Không tìm được user đủ tiền cho {door} {amount}. Hủy phiên."
            print(msg)
            send_telegram(msg)
            return []

        current_bal = balances[chosen]

        # ----- V2: đánh lẻ 1 lần/phiên để tổng cược ngày khác bội 10k (khung 01:00–23:59) -----
        tz = ZoneInfo("Asia/Ho_Chi_Minh")
        now_t = datetime.now(tz).time()
        in_v2_odd_window = dt_time(1, 0) <= now_t <= dt_time(23, 59)
        if (
            in_v2_odd_window
            and today_bets
            and chosen in PRIORITY_USERS_V2
            and chosen not in odd_applied_v2
            and today_bets.get(chosen, 0) % 10000 == 0
            and amount % 10000 == 0
        ):
            add = random.randint(1, 9999)
            if current_bal >= amount + add:
                amount += add
                after = current_bal - amount
                odd_applied_v2.add(chosen)
                print(f"   [V2 odd] {chosen} +{add} → cược {amount} (tổng ngày khác bội 10k)")

        cfg = load_config()
        try:
            all_in_flag = int(cfg.get("ALL_IN_IF_REMAIN_LT_10K", 1))
        except Exception:
            all_in_flag = 1

        # ----- Dư < 10k thì đánh hết (theo config) -----
        if all_in_flag == 1 and current_bal - amount < 10000:
            amount = current_bal
            after = 0

        used.add(chosen)
        balances[chosen] = after

        delay = random.randint(5, 25)
        final.append((chosen, amount, door, delay))

        print(
            f"➡️  User {chosen.ljust(20)} "
            f"Balance={str(current_bal).rjust(8)} "
            f"→ Đặt {door.ljust(3)} {str(amount).rjust(7)} "
            f"(Còn lại {str(after).rjust(8)}) "
            f"Sau {str(delay).rjust(3)}s"
        )

    return final


def run_assigner(online_users: List[str], strategy: int = None) -> List[Tuple[str, int, str, int]]:
    """
    Nếu 'strategy' không truyền vào => sẽ lấy theo TIME_WINDOWS (nếu có), ngược lại dùng root config.
    distribute_for_devices() đã tự xử lý PAUSE và BET_RANGE theo giờ.
    """
    cfg = load_config()
    w = _get_active_window(cfg)

    # Nếu khung giờ đang PAUSE, bỏ qua luôn từ đầu (đề phòng code chỗ khác gọi thẳng run_assigner)
    if w.get("PAUSE"):
        msg = "⏸️ PAUSE theo khung giờ: không chạy run_assigner."
        print(msg)
        return []

    try:
        from jackpot_night_extend import (
            format_jackpot_gate_skip_reason,
            jackpot_periodic_gate_allows_betting as _jp_gate,
        )
    except ImportError:
        _jp_gate = None  # type: ignore
        format_jackpot_gate_skip_reason = None  # type: ignore
    if _jp_gate and not _jp_gate(cfg):
        if format_jackpot_gate_skip_reason:
            print(format_jackpot_gate_skip_reason(cfg), flush=True)
        else:
            print(
                "⏸️ Jackpot: chưa trên JACKPOT_THRESHOLD hoặc đã dừng sau nổ hũ → không chạy run_assigner.",
                flush=True,
            )
        return []

    # Lọc user đang chờ rút để không đặt cược nữa
    online_users = [u for u in online_users if not is_user_waiting_to_withdraw(u)]
    if not online_users:
        return []

    # Lấy strategy theo giờ nếu caller không truyền
    if strategy is None:
        strategy = _strategy_from(cfg, w, fallback=1)
    # Chế độ Cược tuần: ép strategy 6 (ưu tiên tổng cược tuần thấp nhất), V2 do scheduler cập nhật
    if weekly_bet_mode_forces_strategy(cfg):
        strategy = 6

    # Lấy danh sách bets từ chiaTien_Tho (đã áp khung giờ & pause)
    bets = distribute_for_devices([{}] * len(online_users))
    if not bets:
        # Không có bet để gán (pause hoặc BET_RANGE vô hiệu)
        return []

    final_bets = assign_bets(bets, online_users, strategy=strategy)
    if not final_bets:
        return []

    total_tai = sum(amt for (_, amt, door, _) in final_bets if door.upper() == "TAI")
    total_xiu = sum(amt for (_, amt, door, _) in final_bets if door.upper() == "XIU")

    print(f"\n📊 Tổng Tài = {total_tai} | Tổng Xỉu = {total_xiu}")
    return final_bets


# ================= HÀNG ĐỢI BET & ENQUEUE API =================

async def enqueue_bets(final_bets):
    """
    Đặt lịch đẩy lệnh bet vào queue bằng loop.call_later (không tạo task ngủ).
    Lưu handles vào active_ws[user]["pending_schedules"] để dọn khi đóng WS.
    """
    if not final_bets:
        return

    async def enqueue_one(user, amount, door, delay):
        ws_entry = active_ws.get(user)
        if not ws_entry:
            print(f"⚠️ Không tìm thấy ws_entry cho user {user}")
            return
        q: asyncio.Queue = ws_entry["queue"]
        bet_type = "TAI" if door.upper() == "TAI" else "XIU"
        payload = ("bet", {"type": bet_type, "amount": amount})
        try:
            await asyncio.sleep(delay)
            q.put_nowait(payload)
            # print(f"[ENQUEUE] {user} đã nhận lệnh bet {bet_type} {amount} sau {delay}s")
        except Exception as e:
            print(f"⚠️ Lỗi enqueue bet cho {user}: {e}")

    tasks = [asyncio.create_task(enqueue_one(user, amount, door, delay)) for user, amount, door, delay in final_bets]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        # Nếu bị hủy giữa chừng -> không ảnh hưởng các task đã chạy
        # print("⚠️ enqueue_bets bị cancel, một số lệnh bet có thể chưa được đẩy.")
        raise


if __name__ == "__main__":
    online_users = ["trautuankiet", "mayman892", "taimom64", "t0569881312", "trandang64"]

    print("\n=== Theo TIME_WINDOWS (nếu có) ===")
    run_assigner(online_users)

    print("\n=== Ép chiến lược 6 (bỏ qua TIME_WINDOWS) ===")
    run_assigner(online_users, strategy=6)

    print("\n=== Ép chiến lược 7 (bỏ qua TIME_WINDOWS) ===")
    run_assigner(online_users, strategy=7)

    print("\n=== Ép chiến lược 8 (bỏ qua TIME_WINDOWS) ===")
    run_assigner(online_users, strategy=8)

    print("\n=== Ép chiến lược 9 (bỏ qua TIME_WINDOWS) ===")
    run_assigner(online_users, strategy=9)

    print("\n=== Ép chiến lược 10 (bỏ qua TIME_WINDOWS) ===")
    run_assigner(online_users, strategy=10)
