import json
import asyncio
import requests
import time
from constants import allowed_events, active_ws
import constants  # dùng constants.session_seen (tránh global cục bộ)
from chiaTien_Acc import run_assigner, enqueue_bets
from fetch_transactions import fetch_transactions

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
            print(f"⚠️ Lỗi ghi bet-history: {r.text}")
    except Exception as e:
        print(f"⚠️ Không kết nối được API bet-history: {e}")

# ------------------- Cập nhật balance -------------------
def update_balance(user, balance, *, silent=False):
    if balance is None:
        return
    try:
        r = requests.put(f"{API_BASE}/api/users/{user}", json={"balance": balance}, timeout=3)
        if r.status_code == 200 and not silent:
            print(f"💾 [{user}] Cập nhật Balance={balance}")
        elif r.status_code != 200:
            print(f"⚠️ Lỗi update balance API: {r.text}")
    except Exception as e:
        print(f"⚠️ Không kết nối được API users: {e}")

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
            print(f"⚠️ Lỗi update streak: {r.text}")
    except Exception as e:
        print(f"⚠️ Không kết nối được API streaks: {e}")
# ------------------- Xử lý sự kiện -------------------
async def handle_event(user, msg):
    try:
        arr = json.loads(msg[len("42/tx,"):])
    except Exception as e:
        print(f"⚠️ [{user}] Lỗi parse JSON: {e} | raw={msg}")
        return

    if not isinstance(arr, list) or not arr:
        print(f"⚠️ [{user}] Gói tin không hợp lệ: {arr}")
        return

    event, data, *rest = (arr + [None, {}])[:3]
    if not isinstance(data, dict):
        data = {}

    # ------------------- Thông tin user -------------------
    if event == "your-info":
        balance = data.get("balance", 0)
        avatar = data.get("avatar", 0)
        update_balance(user, balance)
        entry = active_ws.get(user)
        if entry is not None:
            entry["last_info_at"] = time.time()
        try:
            r = requests.get(f"{API_BASE}/api/users/{user}", timeout=3)
            if r.status_code == 200:
                acc = r.json()
                nickname = acc.get("nickname")
                if not nickname:
                    requests.put(f"{API_BASE}/api/users/{user}", json={
                        "nickname": data.get("nickname", ""),
                        "avatar": avatar
                    })
                else:
                    requests.put(f"{API_BASE}/api/users/{user}", json={"avatar": avatar})
        except Exception as e:
            print(f"⚠️ [{user}] Lỗi lấy/cập nhật user: {e}")

        try:
            fetch_transactions(user, tx_type="DEPOSIT", limit=10)
        except Exception as e:
            print(f"⚠️ [{user}] Lỗi fetch deposit: {e}")

        await asyncio.sleep(15)

        try:
            fetch_transactions(user, tx_type="WITHDRAW", limit=10)
        except Exception as e:
            print(f"⚠️ [{user}] Lỗi fetch withdraw: {e}")

        return

    # ------------------- Các event khác -------------------
    if event in allowed_events:

        # new-session: chỉ cho phép 1 acc đầu tiên xử lý
        if event == "new-session":
            session_id = data.get("id")
            if constants.session_seen == session_id:
                return
            constants.session_seen = session_id
            print(f"🆕 [{user}] xử lý phiên {session_id}")

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
                            print(f"⚠️ Failed update_streak for {uname}: {e}")

                asyncio.create_task(delayed_lost_check(normalized))

            constants.last_session_id = session_id

            online_users = list(active_ws.keys())
            final_bets = run_assigner(online_users)

            if final_bets:
                entry = active_ws.get(user)
                if entry:
                    old = entry.pop("assign_task", None)
                    if old and not old.done():
                        old.cancel()
                        try:
                            await old
                        except Exception:
                            pass
                    entry["assign_task"] = asyncio.create_task(enqueue_bets(final_bets))

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
                    f"| Số dư mới = {str(post_balance).rjust(10)}"
                )
            else:
                print(
                    f"✅ [{user.ljust(15)}] "
                    f"Đặt cược {bet_label.ljust(4)} "
                    f"- {str(amount).rjust(8)}"
                )

            record_bet(user, game="LC79", amount=amount, door=bet_label,
                       status="success", balance=post_balance)

        elif event == "won-session":
            balance = data.get("balance")
            prize = data.get("prize", 0)
            dices = data.get("dices", [])
            update_balance(user, balance, silent=True)
            print(f"🎲 [{user}] Thắng phiên | Dices={dices} | Prize={prize} | Balance={balance}")
            record_bet(user, game="LC79",
                       amount=data.get("amount", 0),
                       door=data.get("door", ""),
                       status="won", balance=balance, prize=prize, dices=dices)
            # --- Xoá user khỏi list prev_session_users để không bị delayed lost ---
            if constants.last_session_id in prev_session_users:
                if user in prev_session_users[constants.last_session_id]:
                    prev_session_users[constants.last_session_id].remove(user)
            update_streak(user, "won")

        elif event == "lost-session":
            balance = data.get("balance")
            prize = data.get("prize", 0)
            dices = data.get("dices", [])
            update_balance(user, balance, silent=True)
            print(f"🎲 [{user}] Thua phiên | Dices={dices} | Prize={prize} | Balance={balance}")
            record_bet(user, game="LC79",
                       amount=data.get("amount", 0),
                       door=data.get("door", ""),
                       status="lost", balance=balance, prize=prize, dices=dices)
