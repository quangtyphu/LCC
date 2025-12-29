import json
import asyncio
import requests
import time
from constants import allowed_events, active_ws
import constants  # dùng constants.session_seen (tránh global cục bộ)
from chiaTien_Acc import run_assigner, enqueue_bets

API_BASE = "http://127.0.0.1:3000"  # URL server.js của bạn

# ------------------- Phiên trước để kiểm tra streak -------------------
prev_session_users = {}  # {session_id: [username1, username2,...]}

# ------------------- Lưu lịch sử cược -------------------
def record_bet(username, game, amount, door, status="placed", balance=None, prize=None, dices=None):
    payload = {
        "username": username,
        "game": game,
        "amount": amount,
        "door": door,
        "status": status,
    }
    if balance is not None:
        payload["balance"] = balance
    if prize is not None:
        payload["prize"] = prize
    if dices is not None:
        payload["dices"] = dices

    try:
        r = requests.post(f"{API_BASE}/api/bet-history", json=payload, timeout=3)
        if r.status_code != 200:
            print(f"⚠️ Lỗi ghi bet-history: {r.text}", flush=True)
    except Exception as e:
        print(f"⚠️ Không kết nối được API bet-history: {e}", flush=True)

# ------------------- Cập nhật balance -------------------
def update_balance(user, balance, *, silent=False):
    if balance is None:
        return
    try:
        r = requests.put(f"{API_BASE}/api/users/{user}", json={"balance": balance}, timeout=3)
        if r.status_code == 200 and not silent:
            print(f"💾 [{user}] Cập nhật Balance={balance}", flush=True)
        elif r.status_code != 200:
            print(f"⚠️ Lỗi update balance API: {r.text}", flush=True)
    except Exception as e:
        print(f"⚠️ Không kết nối được API users: {e}", flush=True)

# ------------------- Cập nhật streak -------------------
def update_streak(username, result):
    """
    result: 'won' hoặc 'lost'
    """
    # normalize username -> string
    if isinstance(username, str):
        uname = username
    elif isinstance(username, (list, tuple)) and username:
        uname = str(username[0])
    elif isinstance(username, dict):
        uname = str(username.get("username") or username.get("user") or json.dumps(username))
    else:
        uname = str(username)

    try:
        r = requests.post(f"{API_BASE}/streaks/update", json={
            "username": uname,
            "result": result
        }, timeout=3)
        # Bỏ log thành công; chỉ in lỗi khi API trả lỗi
        if r.status_code != 200:
            print(f"⚠️ Lỗi update streak: {r.text}", flush=True)
    except Exception as e:
        print(f"⚠️ Không kết nối được API streaks: {e}", flush=True)
# ------------------- Xử lý sự kiện -------------------
async def handle_event(user, msg):
    try:
        arr = json.loads(msg[len("42/tx,"):])
    except Exception as e:
        print(f"⚠️ [{user}] Lỗi parse JSON: {e} | raw={msg}", flush=True)
        return

    if not isinstance(arr, list) or not arr:
        print(f"⚠️ [{user}] Gói tin không hợp lệ: {arr}", flush=True)
        return

    event, data, *rest = (arr + [None, {}])[:3]
    if not isinstance(data, dict):
        data = {}

    # ------------------- Thông tin user -------------------
    if event == "your-info":
        balance = data.get("money") or data.get("balance") or 0
        try:
            await asyncio.to_thread(
                lambda: requests.put(f"{API_BASE}/api/users/{user}", json={"balance": int(balance)}, timeout=5)
            )
            print(f"💾 [{user}] Cập nhật Balance={int(balance)}", flush=True)
        except Exception:
            pass

        # Debounce: tránh fetch lặp trong 30s
        entry = active_ws.get(user) or {}
        now = time.time()
        last_fetch = entry.get("last_fetch_at", 0)
        if now - last_fetch < 30:
            return
        entry["last_fetch_at"] = now

        async def fetch_bg():
            try:
                from fetch_transactions import fetch_transactions_async
                await fetch_transactions_async(user, "DEPOSIT", 10)
                await fetch_transactions_async(user, "WITHDRAW", 10)
            except Exception as e:
                print(f"⚠️ [{user}] Lỗi fetch tx: {e}", flush=True)

        asyncio.create_task(fetch_bg())
        return

    # ------------------- Các event khác -------------------
    if event in allowed_events:

        # new-session: chỉ cho phép 1 acc đầu tiên xử lý
        if event == "new-session":
            session_id = data.get("id")
            if constants.session_seen == session_id:
                return
            constants.session_seen = session_id
            print(f"🆕 [{user}] xử lý phiên {session_id}", flush=True)

            # --- Kiểm tra phiên trước để update lost nếu chưa nhận win ---
            if hasattr(constants, "last_session_id") and constants.last_session_id in prev_session_users:
                previous_users = prev_session_users.pop(constants.last_session_id)
                # normalize -> danh sách username (chuỗi)
                normalized = []
                for item in previous_users:
                    if isinstance(item, (list, tuple)) and item:
                        normalized.append(str(item[0]))
                    elif isinstance(item, dict):
                        normalized.append(str(item.get("username") or item.get("user") or json.dumps(item)))
                    else:
                        normalized.append(str(item))

                async def delayed_lost_check(users_list):
                    await asyncio.sleep(10)
                    for uname in users_list:
                        try:
                            update_streak(uname, "lost")
                        except Exception as e:
                            print(f"⚠️ Failed update_streak for {uname}: {e}", flush=True)

                asyncio.create_task(delayed_lost_check(normalized))

            constants.last_session_id = session_id

            online_users = list(active_ws.keys())
            final_bets = run_assigner(online_users)

            if final_bets:

                # --- Refactor: Mỗi user có 1 assign_task riêng ---
                for u, amount, door, delay in final_bets:
                    entry_u = active_ws.get(u)
                    if entry_u:
                        old = entry_u.pop("assign_task", None)
                        if old and not old.done():
                            old.cancel()
                            try:
                                await old
                            except Exception:
                                pass
                        # Tạo task enqueue_bets chỉ cho user này
                        entry_u["assign_task"] = asyncio.create_task(enqueue_bets([(u, amount, door, delay)]))

            # --- Lưu user cược của phiên hiện tại ---
            # lưu CHỈ username (chuỗi) để dễ xoá khi có win
            prev_session_users[session_id] = [str(u) for u, *_ in final_bets]

        elif event == "bet-result":
            amount = data.get("amount")
            bet_type = data.get("type", "").upper()
            bet_label = "Tài" if bet_type == "TAI" else "Xỉu"
            post_balance = data.get("postBalance")

            update_balance(user, post_balance, silent=True)

            if post_balance is not None:
                print(
                    f"✅ [{user.ljust(15)}] "
                    f"Đặt cược {bet_label.ljust(4)} "
                    f"- {str(amount).rjust(8)} "
                    f"| Số dư mới = {str(post_balance).rjust(10)}",
                    flush=True
                )
            else:
                print(
                    f"✅ [{user.ljust(15)}] "
                    f"Đặt cược {bet_label.ljust(4)} "
                    f"- {str(amount).rjust(8)}",
                    flush=True
                )

            record_bet(user, game="LC79", amount=amount, door=bet_label,
                       status="success", balance=post_balance)

        elif event == "won-session":
            balance = data.get("balance")
            prize = data.get("prize", 0)
            dices = data.get("dices", [])
            update_balance(user, balance, silent=True)
            print(f"🎲 [{user}] Thắng phiên | Dices={dices} | Prize={prize} | Balance={balance}", flush=True)
            record_bet(user, game="LC79",
                       amount=data.get("amount", 0),
                       door=data.get("door", ""),
                       status="won", balance=balance, prize=prize, dices=dices)
            # --- Xoá user khỏi list prev_session_users để không bị delayed lost ---
            if hasattr(constants, "last_session_id") and constants.last_session_id in prev_session_users:
                if user in prev_session_users[constants.last_session_id]:
                    prev_session_users[constants.last_session_id].remove(user)
            update_streak(user, "won")

        elif event == "lost-session":
            balance = data.get("balance")
            prize = data.get("prize", 0)
            dices = data.get("dices", [])
            update_balance(user, balance, silent=True)
            print(f"🎲 [{user}] Thua phiên | Dices={dices} | Prize={prize} | Balance={balance}", flush=True)
            record_bet(user, game="LC79",
                       amount=data.get("amount", 0),
                       door=data.get("door", ""),
                       status="lost", balance=balance, prize=prize, dices=dices)
