def deposit_full_process(username: str, amount: int, track_history: bool = True) -> dict:
    """
    Thực hiện đầy đủ quy trình nạp tiền:
    - Gọi deposit một lần
    - Không có NDCK vẫn tiếp tục (NDCK rỗng khi gửi bên thứ 3)
    - Lưu DB, lưu QR, tracking giao dịch (nếu bật)
    """
    result = deposit(username, amount)
    payload = (result.get("data") or {}).get("data") or {} if result.get("data") else {}
    api_data = result.get("data") or {}
    transfer_content = _get_transfer_content(payload, api_data) if payload else ""

    if not result.get("ok"):
        err = result.get("error", "API lỗi")
        print(f"⚠️ [{username}] Deposit lỗi: {err} (amount={amount})", flush=True)
        _log_api_response(username, 1, result, payload, api_data)
        return result

    if not payload:
        api_error = api_data.get("message", "API không trả dữ liệu")
        api_code = api_data.get("code", "?")
        print(f"⚠️ [{username}] API không trả dữ liệu (amount={amount})", flush=True)
        _log_api_response(username, 1, result, payload, api_data)
        return {"ok": False, "error": f"[{api_code}] {api_error}"}

    effective_amount = amount
    save_result = save_deposit_to_db(username, result, amount=effective_amount)
    saved = save_result.get("ok")
    order_id = save_result.get("orderId")
    # Lưu QR
    img_path = save_qr_image(payload, username)
    # Bỏ log order_id
    # Tracking giao dịch (nếu lưu DB thành công)
    if track_history and saved and order_id:
        import threading
        threading.Thread(
            target=wait_and_check_deposit,
            args=(username, transfer_content, order_id, effective_amount),
            daemon=True
        ).start()
    return {
        "ok": True,
        "message": "Tạo lệnh nạp tiền thành công (full process)",
        "data": {
            "username": username,
            "amount": effective_amount,
            "accountNumber": payload.get('receiver', ''),
            "bank": payload.get('type', ''),
            "accountHolder": payload.get('name', ''),
            "transferContent": transfer_content,
            "qrBase64": payload.get('qr_base64') or payload.get('qr', ''),
            "qrLink": payload.get('qr_link', ''),
            "qrImagePath": img_path,
            "savedToDB": saved,
            "orderId": order_id
        }
    }
import sys
import io
import os

# Disable buffering cho CMS
os.environ['PYTHONUNBUFFERED'] = '1'

# Fix encoding cho Windows console
if sys.platform == 'win32':
    os.system('chcp 65001 > nul')

import os, re, base64, requests, time
from datetime import datetime
from game_api_helper import game_request_with_retry
from check_deposit_history import check_deposit_history
from telegram_notifier import send_telegram

# Dùng cấu hình chung nếu có, fallback localhost
try:
    from fetch_transactions import NODE_SERVER_URL
except Exception:
    NODE_SERVER_URL = "http://127.0.0.1:3000"

DEPOSIT_URL = "https://gameapi.tele68.com/v1/payment-app/cash-in/bank"
QR_DIR = os.path.join(os.path.dirname(__file__), "qr_outputs")

def _ensure_qr_dir():
    os.makedirs(QR_DIR, exist_ok=True)

def _sanitize_filename(text: str) -> str:
    s = (text or "").strip()
    s = re.sub(r'[\\/:*?"<>|]', "_", s)   # ký tự cấm Windows
    s = re.sub(r"\s+", "_", s)            # khoảng trắng -> _
    return (s[:80] or "no_msg")           # giới hạn độ dài

def _log_api_response(username: str, attempt: int, result: dict, payload: dict, api_data: dict):
    """In dữ liệu API nhận được khi thất bại (không có NDCK) để debug."""
    import json
    # Copy để không in base64 (quá dài)
    def _sanitize_for_log(d):
        if not d:
            return d
        out = {}
        for k, v in d.items():
            if k in ("qr", "qr_base64", "qrBase64") and isinstance(v, str):
                out[k] = f"<base64 {len(v)} chars>" if v else v
            elif isinstance(v, dict):
                out[k] = _sanitize_for_log(v)
            else:
                out[k] = v
        return out
    to_log = {"ok": result.get("ok"), "error": result.get("error")}
    if api_data:
        to_log["data"] = _sanitize_for_log(api_data)
    print(f"   📥 [{username}] Lần {attempt} - API trả về: {json.dumps(to_log, ensure_ascii=False)[:1200]}", flush=True)

def save_qr_image(payload: dict, username: str) -> str | None:
    """
    Lưu ảnh QR ra PNG với tên: username_NDCK.png
    Ưu tiên base64; fallback tải từ qr_link.
    """
    _ensure_qr_dir()
    safe_msg = _sanitize_filename(_get_transfer_content(payload))
    filename = f"{username}_{safe_msg}.png"
    out_path = os.path.join(QR_DIR, filename)

    # 1) Base64 trước
    b64 = payload.get("qr") or payload.get("qr_base64")
    if b64:
        try:
            if isinstance(b64, str) and b64.startswith("data:image"):
                b64 = b64.split(",", 1)[1]
            with open(out_path, "wb") as f:
                f.write(base64.b64decode(b64))
            return out_path
        except Exception:
            pass

    # 2) Fallback: tải từ qr_link
    qr_link = payload.get("qr_link")
    if qr_link:
        try:
            r = requests.get(qr_link, timeout=20)
            if r.ok:
                with open(out_path, "wb") as f:
                    f.write(r.content)
                return out_path
        except Exception:
            pass

    return None

def update_deposit_order_status(order_id: int, status: str) -> bool:
    """
    Cập nhật trạng thái lệnh nạp tiền trong DB.
    
    Args:
        order_id: ID của lệnh nạp trong deposit-orders
        status: Trạng thái mới ("Chờ Nạp"|"Đang Nạp"|"Đã Nạp"|"Thành Công"|"Thất Bại"|"Huỷ")
    """
    try:
        r = requests.put(
            f"{NODE_SERVER_URL}/api/deposit-orders/{order_id}",
            json={"status": status},
            timeout=5
        )
        return r.status_code in (200, 204)
    except Exception as e:
        print(f"⚠️ Lỗi cập nhật trạng thái order: {e}")
        return False


def wait_and_check_deposit(username: str, transfer_content: str, order_id: int, expected_amount: int) -> bool:
    """
    Chờ và check lịch sử nạp tiền theo chu kỳ.

    So khớp: đúng số tiền (không dung sai), NDCK không bắt buộc.

    Args:
        username: Username
        transfer_content: NDCK (chỉ dùng cho log/Telegram khi thất bại)
        order_id: ID lệnh nạp trong deposit-orders
        expected_amount: Số tiền nạp cần khớp

    Returns:
        True nếu tìm thấy giao dịch khớp số tiền, False nếu không
    """
    # Thời gian check: 30s, 60s, 90s, 120s, 600s (10 phút)
    check_intervals = [50, 30,30,30, 30, 120,240,240]  # Tổng: 30, 60, 90, 120, 600s
    
    # Bỏ log bắt đầu theo dõi
    
    for i, wait_time in enumerate(check_intervals, 1):
        time.sleep(wait_time)
        
        elapsed = sum(check_intervals[:i])
        
        # Retry 3 lần nếu gặp lỗi SSL/network
        for retry in range(3):
            try:
                # Gọi check_deposit_history với limit=20 để tăng khả năng tìm thấy
                result = check_deposit_history(username, limit=20, status="SUCCESS")
                
                if not result.get("ok"):
                    print(f"⚠️ [{username}] Không lấy được lịch sử, tiếp tục chờ...")
                    break
                
                # Khớp đúng số tiền (không NDCK)
                transactions = result.get("transactions", [])
                for tx in transactions:
                    tx_amount = int(tx.get("amount") or 0)
                    if tx_amount == int(expected_amount):
                        # Cập nhật trạng thái order sang COMPLETED
                        if update_deposit_order_status(order_id, "Thành Công"):
                            # Chỉ xóa cache khi đã confirm tiền vào (Thành Công)
                            try:
                                from auto_deposit_on_out_of_money import remove_from_deposit_cache
                                remove_from_deposit_cache(username)
                            except Exception as e:
                                print(f"⚠️ [{username}] Không xóa được khỏi cache: {e}")
                        else:
                            print(f"⚠️ [{username}] Không cập nhật được trạng thái order")
                        
                        return True
                
                # Thành công nhưng không tìm thấy giao dịch → thoát retry loop
                break
                
            except Exception as e:
                if retry < 2:
                    print(f"⚠️ [{username}] Lỗi check lịch sử (retry {retry+1}/3): {str(e)[:100]}")
                    time.sleep(5)  # Chờ 5s trước khi retry
                else:
                    print(f"❌ [{username}] Lỗi check lịch sử sau 3 lần thử: {str(e)[:100]}")
                    break
        
        # Không tìm thấy, tiếp tục
        if i < len(check_intervals):
            pass
    
    # Hết 5 lần vẫn không thấy → Cập nhật trạng thái FAILED
    print(f"❌ [{username}] Không tìm thấy giao dịch sau 10 phút")
    
    if update_deposit_order_status(order_id, "Thất Bại"):
        print(f"❌ [{username}] Đã cập nhật lệnh nạp #{order_id} → Thất Bại")
        # Thất bại thì xóa cache để cho phép tạo lại
        try:
            from auto_deposit_on_out_of_money import remove_from_deposit_cache
            remove_from_deposit_cache(username)
        except Exception as e:
            print(f"⚠️ [{username}] Không xóa được khỏi cache: {e}")
        
        # Gửi thông báo Telegram khi thất bại
        try:
            telegram_msg = (
                f"❌ LỆNH NẠP TIỀN THẤT BẠI\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 Username: {username}\n"
                f"🆔 Order ID: #{order_id}\n"
                f"💰 Số tiền: {expected_amount:,}đ\n"
                f"📝 NDCK: {transfer_content}\n"
                f"⏰ Thời gian: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Không tìm thấy giao dịch sau 10 phút theo dõi."
            )
            send_telegram(telegram_msg)
        except Exception as e:
            print(f"⚠️ [{username}] Lỗi gửi Telegram: {e}")
    else:
        print(f"⚠️ [{username}] Không cập nhật được trạng thái order")
    
    return False

def deposit(username: str, amount: int) -> dict:

    if not username or amount <= 0:
        return {"ok": False, "error": "Thiếu username hoặc amount không hợp lệ"}

    # Build params cho API nạp tiền
    params = {"amount": int(amount)}

    # Bỏ log tạo lệnh nạp
    try:
        resp = game_request_with_retry(username, "GET", DEPOSIT_URL, params=params, timeout=30)

        if not resp:
            print(f"❌ [{username}] Không nhận được response từ API", flush=True)
            return {"ok": False, "error": "Không gọi được API nạp tiền"}

        result = {"ok": resp.ok, "status": resp.status_code}
        try:
            result["data"] = resp.json()
        except Exception as e:
            print(f"⚠️ [{username}] Không parse được JSON: {e}", flush=True)
            result["text"] = resp.text

        return result

    except Exception as e:
        print(f"❌ [{username}] Lỗi khi gọi deposit API: {e}")
        import traceback
        traceback.print_exc()
        return {"ok": False, "error": str(e)}

# Các giá trị KHÔNG phải NDCK (thông báo API), bị bỏ qua khi lấy NDCK
_NDCK_EXCLUDE = frozenset(s.lower() for s in (
    "success", "ok", "successful", "thành công", "Thành công", "THÀNH CÔNG",
    "fail", "failed", "error", "lỗi", "pending", "processing"
))

def _get_transfer_content(payload: dict, api_data: dict = None) -> str:
    """Lấy NDCK từ payload. Hỗ trợ msg, transferContent (format BC...). Bỏ qua message/content là thông báo API."""
    for d in (payload, api_data or {}):
        if not d:
            continue
        for key in ("msg", "transferContent", "transfer_content", "message", "content"):
            val = d.get(key)
            if val and isinstance(val, str):
                val = val.strip()
                if not val:
                    continue
                # Bỏ qua nếu là thông báo API (success, ok, ...) không phải NDCK
                if val.lower() in _NDCK_EXCLUDE:
                    continue
                return val
    return ""


def save_deposit_to_db(username: str, api_result: dict, status: str = "pending", amount: int = None) -> dict:
    """
    Lưu lệnh nạp tiền vào DB với trạng thái pending.
    
    Returns:
        dict: {ok: bool, orderId: int} - orderId để tracking sau này
    """
    api_data = api_result.get("data", {}) or {}
    payload = api_data.get("data", {}) or {}
    ndck = _get_transfer_content(payload, api_data)
    rec = {
        "username": username,
        "amount": amount,
        "accountNumber": payload.get("receiver", ""),
        "bank": payload.get("type", ""),
        "accountHolder": payload.get("name", ""),
        "transferContent": ndck,
        "transfer_content": ndck,  # Node/Prisma có thể dùng snake_case cho cột DB
    }
    try:
        # Log để debug: NDCK format BC... (có dấu -) có thể bị Node/DB từ chối
        if ndck and ("-" in ndck or ndck.startswith("BC")):
            print(f"📤 Lưu DB: transferContent={ndck[:50]}... (len={len(ndck)})", flush=True)
        r = requests.post(f"{NODE_SERVER_URL}/api/deposit-orders", json=rec, timeout=5)
        if r.status_code in (200, 201):
            data = r.json()
            return {"ok": True, "orderId": data.get("id")}
        print(f"⚠️ Lưu DB thất bại - status {r.status_code}: {r.text}", flush=True)
        return {"ok": False}
    except Exception as e:
        print(f"⚠️ Lỗi lưu DB: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return {"ok": False}

if __name__ == "__main__":
    import json
    
    # Nếu có arguments từ command line -> mode API (trả JSON)
    if len(sys.argv) >= 3:
        try:
            username = sys.argv[1]
            amount = int(sys.argv[2])
            result = deposit(username, amount)
            if not result.get("ok"):
                print(json.dumps(result, ensure_ascii=False))
                sys.exit(1)
            payload = result.get("data", {}).get("data", {}) or {}
            if not payload:
                api_error = result.get("data", {}).get("message", "API không trả dữ liệu")
                api_code = result.get("data", {}).get("code", "?")
                error_result = {
                    "ok": False,
                    "error": f"[{api_code}] {api_error}"
                }
                print(json.dumps(error_result, ensure_ascii=False))
                sys.exit(1)
            # Lưu DB và QR
            save_result = save_deposit_to_db(username, result, amount=amount)
            saved = save_result.get("ok")
            order_id = save_result.get("orderId")
            img_path = save_qr_image(payload, username)
            # In log đẹp với icon
            print()
            print(f"🎮 User: {username}", flush=True)
            print(f"👤 Tên TK: {payload.get('name', '')}", flush=True)
            print(f"🏦 Số TK: {payload.get('receiver', '')}", flush=True)
            print(f"💰 Số tiền: {amount:,} đ", flush=True)
            print(f"📝 Nội dung: \033[1;31m{_get_transfer_content(payload, result.get('data',{}))}\033[0m", flush=True)
            # Bỏ log order_id
            print()
            # Trả kết quả JSON
            success_result = {
                "ok": True,
                "message": "Tạo lệnh nạp tiền thành công",
                "data": {
                    "username": username,
                    "amount": amount,
                    "accountNumber": payload.get('receiver', ''),
                    "bank": payload.get('type', ''),
                    "accountHolder": payload.get('name', ''),
                    "transferContent": _get_transfer_content(payload, result.get('data',{})),
                    "qrBase64": payload.get('qr_base64') or payload.get('qr', ''),
                    "qrLink": payload.get('qr_link', ''),
                    "qrImagePath": img_path,
                    "savedToDB": saved,
                    "orderId": order_id
                }
            }
            print(json.dumps(success_result, ensure_ascii=False), flush=True)
            # Tracking chạy BACKGROUND (không block response)
            if saved and order_id:
                import subprocess
                transfer_content = _get_transfer_content(payload, result.get('data',{}))
                subprocess.Popen(
                    ['python', __file__, '--track', username, transfer_content, str(order_id), str(amount)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                )
            sys.exit(0)
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False), flush=True)
            sys.exit(1)
    
    # Mode tracking background
    elif len(sys.argv) >= 5 and sys.argv[1] == '--track':
        username = sys.argv[2]
        transfer_content = sys.argv[3]
        order_id = int(sys.argv[4])
        amount = int(sys.argv[5])
        
        # Chạy tracking (10 phút)
        wait_and_check_deposit(username, transfer_content, order_id, amount)
        sys.exit(0)
    
    # Mode interactive (không có arguments)
    else:
        u = input("Username: ").strip()
        a = int(input("Amount: ").strip() or "0")
        
        result = deposit(u, a)

        if not result.get("ok"):
            print(f"❌ Lỗi: {result.get('error', 'Unknown error')}", flush=True)
        else:
            payload = result.get("data", {}).get("data", {}) or {}
            
            if not payload:
                api_error = result.get("data", {}).get("message", "API không trả dữ liệu")
                api_code = result.get("data", {}).get("code", "?")
                print(f"❌ Lỗi API: [{api_code}] {api_error}", flush=True)
            else:
                save_result = save_deposit_to_db(u, result, amount=a)
                saved = save_result.get("ok")
                order_id = save_result.get("orderId")
                
                img_path = save_qr_image(payload, u)
                if not img_path:
                    print("❌ Không lấy được ảnh QR (thiếu base64 và qr_link).", flush=True)
                else:
                    print("✅ Nạp thành công (đã lưu lệnh pending).", flush=True)
                    print(f"   Username: {u}", flush=True)
                    print(f"   STK nhận: {payload.get('receiver', '')}", flush=True)
                    print(f"   Ngân hàng: {payload.get('type', '')}", flush=True)
                    print(f"   Tên: {payload.get('name', '')}", flush=True)
                    print(f"   NDCK: {_get_transfer_content(payload)}", flush=True)
                    print(f"   Ảnh QR: {img_path}", flush=True)
                    print(f"   Lưu DB: {'OK' if saved else 'Lỗi lưu'}", flush=True)
                    # Bỏ log order_id
                    # Chờ và check lịch sử nạp tiền
                    if saved and order_id:
                        transfer_content = _get_transfer_content(payload)
                        wait_and_check_deposit(u, transfer_content, order_id, a)
