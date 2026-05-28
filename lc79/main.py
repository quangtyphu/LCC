"""LC79 main — cwd và sys.path trỏ thư mục lc79/."""
import os
import sys
from pathlib import Path

_LC79_DIR = Path(__file__).resolve().parent
os.chdir(_LC79_DIR)
if str(_LC79_DIR) not in sys.path:
    sys.path.insert(0, str(_LC79_DIR))

from flask import Flask, request, jsonify
from flask_cors import CORS
from deposit_api import deposit_full_process
from withdraw import withdraw
import threading
import time
from user_full_check_service import user_full_check_logic
from status_utils import update_status
app = Flask(__name__)
CORS(app)

# Tắt log request của Flask/Werkzeug để tránh spam log từ thiết bị trong LAN
import logging
logging.getLogger("werkzeug").setLevel(logging.ERROR)
# ============================================================
@app.route('/api/user-full-check', methods=['POST'])
def user_full_check():
    data = request.get_json() or {}
    username = data.get('username')
    if not username:
        return jsonify({'ok': False, 'error': 'Thiếu username'}), 400
    results = user_full_check_logic(username)
    return jsonify({'ok': True, 'results': results})
# =============== API RÚT TIỀN TỪ CMS =======================
# ============================================================
@app.route("/api/withdraw", methods=["POST"])
def api_withdraw():
    data = request.get_json() or {}
    username = data.get("username") or data.get("user")
    amount = data.get("amount")
    if not username or not amount:
        return jsonify({"error": "Thiếu username hoặc amount"}), 400

    def run_withdraw():
        try:
            withdraw(username, int(amount))
        except Exception as e:
            print(f"[API] Lỗi rút tiền cho {username}: {e}", flush=True)

    threading.Thread(
        target=run_withdraw,
        daemon=True
    ).start()

    return jsonify({"ok": True, "message": f"Đang thực hiện rút tiền cho {username}"}), 200

# API nạp tiền (full process)
@app.route('/api/deposit', methods=['POST'])
def api_deposit():
    try:
        data = request.get_json() or {}
        username = data.get('username')
        amount = data.get('amount')
        
        if not username:
            return jsonify({"ok": False, "error": "Thiếu username"}), 400
        if not amount or amount <= 0:
            return jsonify({"ok": False, "error": "Thiếu amount hoặc amount không hợp lệ"}), 400
        
        result = deposit_full_process(username, amount)
        return jsonify(result)
    except Exception as e:
        import traceback
        error_msg = str(e)
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"Lỗi server: {error_msg}"}), 500


@app.route('/api/deposit/send-third-party', methods=['POST'])
def api_deposit_send_third_party():
    """CMS: gửi lệnh nạp đã tạo (popup QR) cho bên thứ 3."""
    try:
        from third_party_deposit_handler import send_existing_order_to_third_party

        data = request.get_json() or {}
        username = (data.get('username') or '').strip()
        if not username:
            return jsonify({"ok": False, "error": "Thiếu username"}), 400
        result = send_existing_order_to_third_party(username, data)
        if not result.get("ok"):
            return jsonify(result), 500
        return jsonify(result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"Lỗi server: {e}"}), 500

import os
import sys
os.environ['PYTHONUNBUFFERED'] = '1'
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

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

API_BASE = "http://127.0.0.1:3000"  # URL CMS Node.js

# Trạng thái API lần trước (theo user) — chỉ schedule nạp khi *vừa* chuyển sang Hết Tiền,
# không lặp mỗi 20s trong watcher (slot release xong là lên lịch lại → gọi auto_deposit lần 2).
_watcher_last_api_status = {}


# ============================================================
# =============== HÀM CHẠY CHÍNH KHÔNG GIỚI HẠN GIỜ ==========
# ============================================================
async def watcher_loop():
    tz = pytz.timezone("Asia/Ho_Chi_Minh")
    _jackpot_bootstrapped = False

    while True:
        now = datetime.now(tz)

        current = set(active_ws.keys())
        target_accounts = get_active_accounts()
        target = set(acc["username"] for acc in target_accounts)


        # ✅ Ngắt WS nếu trạng thái KHÁC 'Đang Chơi' hoặc là 'Token Lỗi'
        try:
            resp = requests.get(f"{API_BASE}/api/users", timeout=5)
            if resp.status_code == 200:
                users = resp.json()
                for udoc in users:
                    u = udoc.get("username")
                    status = udoc.get("status")
                    if u in current and status != "Đang Chơi":
                        if status == "Hết Tiền":
                            prev = _watcher_last_api_status.get(u)
                            if prev != "Hết Tiền":
                                try:
                                    from streak_deposit_scheduler import (
                                        schedule_het_tien_deposit_after_delay,
                                    )
                                    schedule_het_tien_deposit_after_delay(u)
                                except Exception as ex:
                                    print(f"[WARN] auto/streak deposit ({u}): {ex}", flush=True)
                        await disconnect_user(u)
                    if u in current and status is not None:
                        _watcher_last_api_status[u] = status
                    # Nếu là Token Lỗi thì vẫn tự động refresh JWT như cũ
                    if u in current and status == "Token Lỗi":
                        print(f"🔄 [{u}] Tự động refresh JWT do Token Lỗi", flush=True)
                        new_jwt = refresh_jwt(u)
                        if new_jwt:
                            try:
                                requests.put(f"{API_BASE}/api/users/{u}", json={"jwt": new_jwt}, timeout=5)
                                update_status(u, "Đang Chơi")
                                print(f"✅ [{u}] Đã refresh JWT và cập nhật trạng thái Đang Chơi", flush=True)
                            except Exception as e:
                                print(f"❌ [{u}] Lỗi khi cập nhật JWT mới: {e}", flush=True)
                        else:
                            print(f"❌ [{u}] Refresh JWT thất bại", flush=True)
        except Exception:
            pass

        # Mở WS mới cho user chưa có (giữ nguyên)
        if target_accounts:
            for acc in target_accounts:
                u = acc["username"]
                if u not in active_ws:
                    q = asyncio.Queue()
                    conn_id = uuid.uuid4().hex
                    active_ws[u] = {"queue": q, "task": None, "acc": acc, "conn_id": conn_id}
                    task = asyncio.create_task(handle_ws(acc, conn_id))
                    active_ws[u]["task"] = task

        if not _jackpot_bootstrapped:
            try:
                from constants import load_config
                from jackpot_night_extend import refresh_jackpot_cache

                _done = await asyncio.to_thread(
                    lambda: refresh_jackpot_cache(load_config(), force=True)
                )
                if _done:
                    _jackpot_bootstrapped = True
            except Exception as _jb:
                print(f"[WARN] jackpot bootstrap: {_jb}", flush=True)

        await asyncio.sleep(20)

        try:
            from constants import load_config
            from jackpot_night_extend import refresh_jackpot_cache, try_run_daily_check

            _cfg = load_config()
            await asyncio.to_thread(lambda: refresh_jackpot_cache(_cfg, force=False))
            await asyncio.to_thread(try_run_daily_check, _cfg)
        except Exception as _je:
            print(f"[WARN] jackpot_night_extend: {_je}", flush=True)


# ============================================================
# ====================== TIỆN ÍCH API ========================
# ============================================================
def get_user(username: str):
    try:
        r = requests.get(f"{API_BASE}/api/users/{username}", timeout=5)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None



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

    print(f"\n🚀 FORCE CHECK USER: {username}", flush=True)

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
        print(f"🔌 [{username}] Proxy OK", flush=True)
    except Exception as e:
        print(f"❌ [{username}] Proxy lỗi: {e}", flush=True)
        update_status(username, "Proxy Lỗi")
        return jsonify({"error": "Proxy lỗi"}), 400

    # 2) Kiểm tra + refresh JWT nếu cần
    try:
        ok = asyncio.run(test_token(jwt, proxy, user=username))
    except Exception:
        ok = False

    if not ok:
        print("❌ Token lỗi → Refresh JWT", flush=True)
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
            print(f"🔄 [{username}] Hủy WS cũ", flush=True)
        except Exception:
            pass

    # Đặt cọc
    active_ws[username] = {"connecting": True}

    # Mở WS mới
    acc = user.copy()
    acc["jwt"] = jwt
    run_ws_in_thread(acc, username)
    
    print(f"♻️ [{username}] Force-reconnect WS để cập nhật balance", flush=True)

    return jsonify({"ok": True, "mode": "force-reconnect"}), 200


@app.route("/api/ws-slot-out-of-money", methods=["POST"])
def ws_slot_out_of_money():
    """
    ws_slot_client (subprocess) gọi khi postBalance < ngưỡng dừng quay (WS slot đã đóng ở tiến trình slot):
    chỉ loại user khỏi ưu tiên SLOT_NV — không đụng WS tài xỉu/minigame.
    """
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or data.get("user") or "").strip()
    if not username:
        return jsonify({"ok": False, "error": "Thiếu username"}), 400

    try:
        from slot_near_mission_scheduler import note_slot_nv_balance_skip_from_ws_client

        note_slot_nv_balance_skip_from_ws_client(username)
    except Exception as ex:
        print(f"⚠️ [{username}] SLOT_NV skip sau hết tiền slot: {ex}", flush=True)

    return jsonify({"ok": True}), 200


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
        target=deposit_full_process,
        args=(username,),
        daemon=True
    ).start()
    
    return jsonify({"ok": True, "message": f"Đang check transactions + gift-box cho {username}"}), 200


# ============================================================
# =============== THIRD PARTY DEPOSIT HANDLER ===============
# ============================================================
# Chạy Flask app từ third_party_deposit_handler.py trong thread riêng

def run_third_party_handler():
	"""
	Chạy Flask app từ third_party_deposit_handler.py trong thread riêng.
	"""
	import third_party_deposit_handler

	third_party_deposit_handler.refresh_urls()
	port = third_party_deposit_handler.HANDLER_PORT
	cb = third_party_deposit_handler.CALLBACK_URL
	print(f"💳 LC79 nạp handler :{port} | Banking callback → {cb}", flush=True)
	third_party_deposit_handler.app.run(host="0.0.0.0", port=port, debug=False)


# 🧵 Chạy API song song
def run_api():
    app.run(host="0.0.0.0", port=5006, debug=False, use_reloader=False)


def run_flask():
    print("🚀 Flask API server đang chạy tại http://0.0.0.0:8080 ...", flush=True)
    app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False)


_shutdown_lc79_done = False


def shutdown_lc79_background_services() -> None:
    """
    Giống kỳ vọng khi dừng tài xỉu: hủy WS + dừng quay slot NV.
    Trên Windows, Ctrl+C thường không tới tiến trình con ``ws_slot_client`` — phải terminate thủ công.
    """
    global _shutdown_lc79_done
    if _shutdown_lc79_done:
        return
    _shutdown_lc79_done = True
    try:
        from slot_near_mission_scheduler import terminate_slot_near_mission_subprocesses

        terminate_slot_near_mission_subprocesses()
    except Exception as e:
        print(f"[SHUTDOWN] SLOT_NV: {e}", flush=True)
    try:

        async def _disconnect_all_ws():
            users = list(active_ws.keys())
            for u in users:
                await disconnect_user(u)

        asyncio.run(_disconnect_all_ws())
    except Exception as e:
        print(f"[SHUTDOWN] WS tài xỉu: {e}", flush=True)


if __name__ == "__main__":
    import atexit

    atexit.register(shutdown_lc79_background_services)

    import threading
    from auto_deposit_on_out_of_money import reset_deposit_cache, start_periodic_check
    from daily_active_no_deposit_scheduler import auto_active_no_deposit_scheduler
    from pending_withdraw_checker import start_pending_withdraw_checker
    from weekly_bet_mode_scheduler import start_weekly_bet_mode_scheduler
    from slot_near_mission_scheduler import start_slot_near_mission_scheduler
    from withdraw_threshold_scheduler import start_withdraw_threshold_reset_scheduler

    # Reset cache khi khởi động chương trình (giống như pending_withdrawals reset về {})
    print("[INIT] Đang reset deposit cache...", flush=True)
    reset_deposit_cache()
    
    # Periodic nạp/quét: mỗi 60s (cố định trong auto_deposit_on_out_of_money)
    print("[INIT] Đang khởi động periodic check auto nạp (60s)...", flush=True)
    start_periodic_check()
    
    # Chạy Flask main ở thread riêng
    threading.Thread(target=run_flask, daemon=True).start()
    # Chạy third_party_deposit_handler ở thread riêng
    threading.Thread(target=run_third_party_handler, daemon=True).start()
    # Chạy scheduler nạp tiền user chưa nạp hôm nay (ACTIVE_NO_DEPOSIT_SCHEDULER trong config.json)
    threading.Thread(target=auto_active_no_deposit_scheduler, daemon=True).start()
    # (Đã bỏ streak_deposit_scheduler - thay bằng check streak khi user chuyển Hết Tiền trong chiaTien_Acc)
    # Chạy scheduler check lịch sử rút cho user đang chờ (10 phút)
    start_pending_withdraw_checker(interval_seconds=600)
    # Chế độ Cược tuần: mỗi 60s cập nhật PRIORITY_USERS_V2 khi WEEKLY_BET_MODE.ENABLED=1
    start_weekly_bet_mode_scheduler()
    start_slot_near_mission_scheduler()
    start_withdraw_threshold_reset_scheduler()
    # Chạy watcher_loop như cũ
    try:
        asyncio.run(watcher_loop())
    except KeyboardInterrupt:
        print("\n⏹ Đã dừng chương trình.", flush=True)
    finally:
        shutdown_lc79_background_services()
