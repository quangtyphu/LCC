import random
import asyncio
import requests
import contextlib
from typing import List, Tuple, Dict
from datetime import datetime
from zoneinfo import ZoneInfo

from chiaTien_Tho import distribute_for_devices
from constants import active_ws, load_config
from telegram_notifier import send_telegram

API_BASE = "http://127.0.0.1:3000"  # server.js


# ================= Helpers cấu hình theo khung giờ =================

def _get_active_window(cfg: dict) -> dict:
    """
    Trả về nguyên window đang hiệu lực (inclusive start, exclusive end).
    Hỗ trợ khoảng qua nửa đêm (start > end).
    Không khớp thì trả {}
    """
    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    now = datetime.now(tz).time()
    windows = cfg.get("TIME_WINDOWS") or []

    # parse HH:MM
    from datetime import datetime as dt
    for w in windows:
        s_raw, e_raw = w.get("start"), w.get("end")
        if not s_raw or not e_raw:
            continue
        try:
            s = dt.strptime(s_raw, "%H:%M").time()
            e = dt.strptime(e_raw, "%H:%M").time()
        except Exception:
            continue

        in_range = (s <= now < e) if s < e else (now >= s or now < e)
        if in_range:
            return w
    return {}


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

def _strategy_from(cfg: dict, w: dict, fallback: int = 1) -> int:
    """
    Ưu tiên ASSIGN_STRATEGY trong window nếu là số hợp lệ (1..10).
    Nếu không có/không hợp lệ => dùng root; nếu root không hợp lệ => fallback.
    """
    win_val = w.get("ASSIGN_STRATEGY")
    if isinstance(win_val, int) and 1 <= win_val <= 11:
        return win_val
    try:
        root_val = int(cfg.get("ASSIGN_STRATEGY", fallback))
        if 1 <= root_val <= 11:
            return root_val
    except Exception:
        pass
    return fallback


# ================= Helpers khác =================

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
                    with contextlib.suppress(Exception):
                        requests.put(f"{API_BASE}/api/users/{user}", json={"status": "Hết Tiền"})
                    # Kiểm tra PAUSE trước khi gọi auto_deposit_on_out_of_money
                    config = load_config()
                    active_window = _get_active_window(config)
                    if active_window.get("PAUSE"):
                        print(f"[SKIP] {user} balance < 10000 nhưng đang trong khung giờ PAUSE ({active_window.get('start', 'N/A')}-{active_window.get('end', 'N/A')}), bỏ qua nạp tiền tự động.")
                    else:
                        # Gọi auto_deposit_on_out_of_money
                        try:
                            from auto_deposit_on_out_of_money import auto_deposit_for_user
                            auto_deposit_for_user(user)
                        except Exception as e:
                            print(f"[ERROR] auto_deposit_for_user({user}): {e}")
                # else:
                #     with contextlib.suppress(Exception):
                #         requests.put(f"{API_BASE}/api/users/{user}", json={"status": "Đang Chơi"})
            else:
                balances[user] = 0
                with contextlib.suppress(Exception):
                    requests.put(f"{API_BASE}/api/users/{user}", json={"status": "Hết Tiền"})
        except Exception as e:
            print(f"⚠️ Lỗi lấy balance cho {user}: {e}")
            balances[user] = 0
            with contextlib.suppress(Exception):
                requests.put(f"{API_BASE}/api/users/{user}", json={"status": "Hết Tiền"})
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

    # PAUSE theo khung giờ
    if window.get("PAUSE"):
        msg = "⏸️ PAUSE theo khung giờ: bỏ qua phiên gán cược."
        print(msg)
        return []

    # Lấy PRIORITY_USERS/ASSIGN_STRATEGY theo giờ
    PRIORITY_USERS = _priority_users_from(config, window)  # vẫn dùng cho các strategy khác
    PRIORITY_USERS_V2 = _priority_users_v2_from(config, window)
    PRIORITY_USERS_V3 = _priority_users_v3_from(config, window)

    balances = _fresh_balances_for_online(online_users)
    today_bets = _fetch_today_bets_for_online(online_users) if strategy in (7, 8, 9, 10, 11) else {}
    weekly_bets = _fetch_weekly_bets_for_online(online_users) if strategy in (6, 7, 8) else {}
    monthly_bets = _fetch_monthly_bets_for_online(online_users) if strategy == 5 else {}

    # sort giảm dần theo amount để nhận diện bet lớn nhất
    to_assign = sorted([(amt, door) for (_dev, amt, door) in bets], key=lambda x: -x[0])

    used = set()
    final: List[Tuple[str, int, str, int]] = []



    # ---------------------------- VÒNG GÁN ----------------------------
    for idx, (amount, door) in enumerate(to_assign):
        # ứng viên cho mức amount ở lượt này
        candidates = []
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

        # -------------------- Chiến lược chọn account --------------------
        if strategy == 1:
            after, chosen, _bal = min(candidates, key=lambda t: t[0])  # AFTER thấp nhất

        elif strategy == 2:
            after, chosen, _bal = random.choice(candidates)  # Random

        elif strategy == 3:
            # Ưu tiên PRIORITY_USERS, fallback AFTER thấp nhất
            chosen, after, _bal = None, None, None
            for u in PRIORITY_USERS:
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
            # Ưu tiên balance cao → thấp cho users KHÔNG thuộc V2/V3, sau đó mới đến V2 rồi V3
            others = [u for u in online_users if u not in used and u not in PRIORITY_USERS_V2 and u not in PRIORITY_USERS_V3]
            others_sorted = sorted(others, key=lambda u: -balances.get(u, 0))

            v2_sorted = sorted([u for u in PRIORITY_USERS_V2 if u in online_users and u not in used], key=lambda u: -balances.get(u, 0))
            v3_sorted = sorted([u for u in PRIORITY_USERS_V3 if u in online_users and u not in used], key=lambda u: -balances.get(u, 0))

            ordered = others_sorted + v2_sorted + v3_sorted

            chosen = None
            after = None
            _bal = None
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
            for u in PRIORITY_USERS:
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
            for u in PRIORITY_USERS:
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
            # Ưu tiên V2 -> V3 với tổng cược ngày thấp (giống 9/10/11), còn lại ưu tiên tổng cược tuần cao
            v2_sorted = sorted(
                [u for u in PRIORITY_USERS_V2 if u in online_users and u not in used],
                key=lambda u: (today_bets.get(u, 0), balances.get(u, 0))
            )
            v3_sorted = sorted(
                [u for u in PRIORITY_USERS_V3 if u in online_users and u not in used],
                key=lambda u: (today_bets.get(u, 0), balances.get(u, 0))
            )
            others = [
                u for u in online_users
                if u not in PRIORITY_USERS_V2
                and u not in PRIORITY_USERS_V3
                and u not in used
            ]
            others_sorted = sorted(others, key=lambda u: (-weekly_bets.get(u, 0), -balances.get(u, 0)))

            ordered = v2_sorted + v3_sorted + others_sorted

            chosen = None
            after = None
            _bal = None
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

        elif strategy == 8:
            # Ưu tiên V2 -> V3 với tổng cược ngày thấp (giống 9/10/11), còn lại ưu tiên tổng cược tuần thấp
            v2_sorted = sorted(
                [u for u in PRIORITY_USERS_V2 if u in online_users and u not in used],
                key=lambda u: (today_bets.get(u, 0), balances.get(u, 0))
            )
            v3_sorted = sorted(
                [u for u in PRIORITY_USERS_V3 if u in online_users and u not in used],
                key=lambda u: (today_bets.get(u, 0), balances.get(u, 0))
            )
            others = [
                u for u in online_users
                if u not in PRIORITY_USERS_V2
                and u not in PRIORITY_USERS_V3
                and u not in used
            ]
            others_sorted = sorted(others, key=lambda u: (weekly_bets.get(u, 0), balances.get(u, 0)))

            ordered = v2_sorted + v3_sorted + others_sorted

            chosen = None
            after = None
            _bal = None
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

        elif strategy == 9:
            # nhóm ưu tiên mới
            prio_online = [u for u in PRIORITY_USERS_V2 if u in online_users and u not in used]
            prio_sorted = sorted(prio_online, key=lambda u: (today_bets.get(u, 0), balances.get(u, 0)))

            others = [u for u in online_users if u not in prio_online and u not in used]
            others_sorted = sorted(others, key=lambda u: balances.get(u, 0))

            ordered = prio_sorted + others_sorted

            chosen = None
            after = None
            _bal = None
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

        elif strategy == 10:
            # Ưu tiên các user KHÔNG thuộc PRIORITY_USERS_V2 theo balance tăng dần;
            # nếu thiếu thì dùng PRIORITY_USERS_V2 theo tổng cược ngày thấp nhất
            others = [u for u in online_users if u not in PRIORITY_USERS_V2]
            others_sorted = sorted(others, key=lambda u: balances.get(u, 0))

            prio_sorted = sorted(
                [u for u in PRIORITY_USERS_V2 if u in online_users],
                key=lambda u: (today_bets.get(u, 0), balances.get(u, 0))
            )

            ordered = others_sorted + prio_sorted

            chosen = None
            after = None
            _bal = None
            for u in ordered:
                if u in used:
                    continue
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
            # 1️⃣ User KHÔNG thuộc V2 & V3 → balance tăng dần
            others = [
                u for u in online_users
                if u not in PRIORITY_USERS_V2
                and u not in PRIORITY_USERS_V3
                and u not in used
            ]
            others_sorted = sorted(others, key=lambda u: balances.get(u, 0))

            # 2️⃣ PRIORITY_USERS_V2 → today_bet thấp nhất
            v2_sorted = sorted(
                [u for u in PRIORITY_USERS_V2 if u in online_users and u not in used],
                key=lambda u: (today_bets.get(u, 0), balances.get(u, 0))
            )

            # 3️⃣ PRIORITY_USERS_V3 → today_bet thấp nhất
            v3_sorted = sorted(
                [u for u in PRIORITY_USERS_V3 if u in online_users and u not in used],
                key=lambda u: (today_bets.get(u, 0), balances.get(u, 0))
            )

            ordered = others_sorted + v2_sorted + v3_sorted

            chosen = None
            after = None
            _bal = None

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

        else:
            # fallback an toàn
            after, chosen, _bal = random.choice(candidates)

        # ----- Áp dụng quy tắc "dư < 10k thì đánh hết" (giữ nguyên như bản trước) -----
        current_bal = balances[chosen]
        if current_bal - amount < 10000:
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

    # Lấy strategy theo giờ nếu caller không truyền
    if strategy is None:
        strategy = _strategy_from(cfg, w, fallback=1)

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
