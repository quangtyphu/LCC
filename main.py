import asyncio
import threading
import uuid
from datetime import datetime

import pytz
import requests
from flask import Flask, request, jsonify

from constants import active_ws
from get_active_accounts import get_active_accounts
from ws_connection import handle_ws, disconnect_user
from token_utils import test_token
from jwt_manager import refresh_jwt
from fetch_transactions import check_all_transactions  # ← Đổi import

API_BASE = "http://127.0.0.1:3000"  # URL CMS Node.js
app = Flask(__name__)


# ============================================================
# =============== HÀM CHẠY CHÍNH KHÔNG GIỚI HẠN GIỜ ==========
# ============================================================
async def watcher_loop():
    tz = pytz.timezone("Asia/Ho_Chi_Minh")

    while True:
        now = datetime.now(tz)

        current = set(active_ws.keys())
        target_accounts = get_active_accounts()
        target = set(acc["username"] for acc in target_accounts)

        # ❌ BỎ: Ngắt user không còn trong target (không phụ thuộc trạng thái Đang Chơi nữa)
        # for u in current - target:
        #     await disconnect_user(u)

        # ✅ CHỈ NGẮT NẾU TRẠNG THÁI = "Token Lỗi"
        try:
            resp = requests.get(f"{API_BASE}/api/users", timeout=5)
            if resp.status_code == 200:
                users = resp.json()
                for udoc in users:
                    u = udoc.get("username")
                    status = udoc.get("status")
                    if u in current and status == "Token Lỗi":
                        await disconnect_user(u)
        except Exception:
            pass

        # Mở WS mới cho user chưa có (giữ nguyên)
        if target_accounts:
            for acc in target_accounts:
                u = acc["username"]
                if u not in active_ws:
                    print(f"➕ Mở WS mới cho {u}")
                    q = asyncio.Queue()
                    conn_id = uuid.uuid4().hex
                    active_ws[u] = {"queue": q, "task": None, "acc": acc, "conn_id": conn_id}
                    task = asyncio.create_task(handle_ws(acc, conn_id))
                    active_ws[u]["task"] = task

        await asyncio.sleep(20)


# ============================================================
# ====================== TIỆN ÍCH API ========================
# ============================================================
def get_user(username: str):
    try:
        r = requests.get(f"{API_BASE}/api/users/{username}", timeout=5)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def update_status(username: str, status: str) -> bool:
    try:
        r = requests.put(f"{API_BASE}/api/users/{username}", json={"status": status}, timeout=5)
        return r.status_code == 200
    except Exception:
        return False


# main.py
def run_ws_in_thread(acc: dict, username: str):
    async def runner():
        q = asyncio.Queue()
        conn_id = uuid.uuid4().hex
        active_ws[username] = {"queue": q, "task": None, "acc": acc, "conn_id": conn_id}
        task = asyncio.create_task(handle_ws(acc, conn_id))
        active_ws[username]["task"] = task
        try:
            await task
        except asyncio.CancelledError:
            # WS bị hủy chủ động (disconnect_user, hết tiền, thay thế WS mới...) -> bỏ qua
            pass

    loop = asyncio.new_event_loop()
    threading.Thread(
        target=loop.run_until_complete,
        args=(runner(),),
        daemon=True
    ).start()

# ============================================================
# =============== API CHỦ ĐỘNG FORCE CHECK ===================
# ============================================================
@app.route("/api/force-check", methods=["POST"])
def force_check():
    data = request.get_json() or {}
    username = data.get("username") or data.get("user")
    if not username:
        return jsonify({"error": "Thiếu username"}), 400

    print(f"\n🚀 FORCE CHECK USER: {username}")

    user = get_user(username)
    if not user:
        return jsonify({"error": "Không tìm thấy user"}), 404

    proxy = user.get("proxy")
    jwt = user.get("jwt")

    # 1) Kiểm tra proxy
    if not proxy:
        update_status(username, "Proxy Lỗi")
        return jsonify({"error": "Thiếu proxy"}), 400

    try:
        host, port, userp, passp = proxy.split(":")
        import socks
        sock = socks.socksocket()
        sock.set_proxy(socks.SOCKS5, host, int(port), True, userp, passp)
        sock.settimeout(5)
        sock.connect(("wtx.tele68.com", 443))
        sock.close()
        print(f"🔌 [{username}] Proxy OK")
    except Exception as e:
        print(f"❌ [{username}] Proxy lỗi: {e}")
        update_status(username, "Proxy Lỗi")
        return jsonify({"error": "Proxy lỗi"}), 400

    # 2) Kiểm tra + refresh JWT nếu cần
    try:
        ok = asyncio.run(test_token(jwt, proxy))
    except Exception:
        ok = False

    if not ok:
        print("❌ Token lỗi → Refresh JWT")
        new_jwt = refresh_jwt(username)
        if not new_jwt:
            update_status(username, "Token Lỗi")
            return jsonify({"error": "Token lỗi, refresh thất bại"}), 400
        jwt = new_jwt
        try:
            requests.put(f"{API_BASE}/api/users/{username}", json={"jwt": jwt}, timeout=5)
        except Exception:
            pass

    # 3) Luôn force-reconnect (dù có WS hay không) để chắc chắn lấy balance
    entry = active_ws.get(username)
    if entry and entry.get("task") and not entry["task"].done():
        # Hủy WS cũ
        try:
            entry["task"].cancel()
            print(f"🔄 [{username}] Hủy WS cũ")
        except Exception:
            pass

    # Đặt cọc
    active_ws[username] = {"connecting": True}

    # Mở WS mới
    acc = user.copy()
    acc["jwt"] = jwt
    run_ws_in_thread(acc, username)
    
    print(f"♻️ [{username}] Force-reconnect WS để cập nhật balance")

    return jsonify({"ok": True, "mode": "force-reconnect"}), 200


# ============================================================
# =============== API CHECK NẠP/RÚT + NHẬN QUÀ ===============
# ============================================================
@app.route("/api/check-transactions", methods=["POST"])
def check_transactions():
    data = request.get_json() or {}
    username = data.get("username") or data.get("user")
    if not username:
        return jsonify({"error": "Thiếu username"}), 400

    # Chạy trong thread riêng
    threading.Thread(
        target=check_all_transactions,
        args=(username,),
        daemon=True
    ).start()
    
    return jsonify({"ok": True, "message": f"Đang check transactions + gift-box cho {username}"}), 200


# 🧵 Chạy API song song
def run_api():
    app.run(host="0.0.0.0", port=5006, debug=False, use_reloader=False)


if __name__ == "__main__":
    threading.Thread(target=run_api, daemon=True).start()
    try:
        asyncio.run(watcher_loop())
    except KeyboardInterrupt:
        print("\n⏹ Đã dừng chương trình.")
