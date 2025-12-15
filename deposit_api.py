def deposit_full_process(username: str, amount: int) -> dict:
    """
    Thực hiện đầy đủ quy trình nạp tiền:
    - Gọi deposit
    - Lưu DB
    - Lưu QR
    - Tracking giao dịch
    - Trả kết quả tổng hợp
    """
    result = deposit(username, amount)
    if not result.get("ok"):
        return result
    payload = result.get("data", {}).get("data", {}) or {}
    if not payload:
        api_error = result.get("data", {}).get("message", "API không trả dữ liệu")
        api_code = result.get("data", {}).get("code", "?")
        return {"ok": False, "error": f"[{api_code}] {api_error}"}
    # Lưu DB
    save_result = save_deposit_to_db(username, result)
    saved = save_result.get("ok")
    order_id = save_result.get("orderId")
    # Lưu QR
    img_path = save_qr_image(payload, username)
    # In ra order_id vừa lưu
    print(f"[INFO] order_id (lưu DB): {order_id}", flush=True)
    # Tracking giao dịch (nếu lưu DB thành công)
    if saved and order_id:
        transfer_content = payload.get('msg', '')
        import threading
        threading.Thread(
            target=wait_and_check_deposit,
            args=(username, transfer_content, order_id, amount),
            daemon=True
        ).start()
    return {
        "ok": True,
        "message": "Tạo lệnh nạp tiền thành công (full process)",
        "data": {
            "username": username,
            "amount": amount,
            "accountNumber": payload.get('receiver', ''),
            "accountHolder": payload.get('name', ''),
            "transferContent": payload.get('msg', ''),
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

def save_qr_image(payload: dict, username: str) -> str | None:
    """
    Lưu ảnh QR ra PNG với tên: username_NDCK.png
    Ưu tiên base64; fallback tải từ qr_link.
    """
    _ensure_qr_dir()
    safe_msg = _sanitize_filename(payload.get("msg", ""))
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
        status: Trạng thái mới (success/failed)
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
    Chờ và check lịch sử nạp tiền 5 lần:
    - Sau 30s, 60s, 90s, 120s, 10 phút
    
    Args:
        username: Username
        transfer_content: Nội dung chuyển khoản (NDCK) để so khớp
        order_id: ID lệnh nạp trong deposit-orders
        expected_amount: Số tiền nạp
    
    Returns:
        True nếu tìm thấy giao dịch khớp, False nếu không
    """
    # Thời gian check: 30s, 60s, 90s, 120s, 600s (10 phút)
    check_intervals = [30, 30, 60, 120, 480]  # Tổng: 30, 60, 90, 120, 600s
    
    print(f"⏳ [{username}] Bắt đầu theo dõi lệnh nạp (NDCK: {transfer_content})...")
    
    for i, wait_time in enumerate(check_intervals, 1):
        time.sleep(wait_time)
        
        elapsed = sum(check_intervals[:i])
        print(f"🔍 [{username}] Lần {i}/5 - Sau {elapsed}s: Đang check lịch sử nạp...")
        
        # Retry 3 lần nếu gặp lỗi SSL/network
        for retry in range(3):
            try:
                # Gọi check_deposit_history với limit=20 để tăng khả năng tìm thấy
                result = check_deposit_history(username, limit=20, status="SUCCESS")
                
                if not result.get("ok"):
                    print(f"⚠️ [{username}] Không lấy được lịch sử, tiếp tục chờ...")
                    break
                
                # Tìm giao dịch khớp NDCK và amount
                transactions = result.get("transactions", [])
                for tx in transactions:
                    tx_content = tx.get("content", "")
                    tx_amount = tx.get("amount", 0)
                    
                    if tx_content == transfer_content and tx_amount == expected_amount:
                        print(f"✅ [{username}] Tìm thấy giao dịch khớp! Amount: {tx_amount:,}đ, NDCK: {tx_content}")
                        
                        # Cập nhật trạng thái order sang COMPLETED
                        if update_deposit_order_status(order_id, "completed"):
                            print(f"✅ [{username}] Đã cập nhật lệnh nạp #{order_id} → COMPLETED")
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
            print(f"⏳ [{username}] Chưa thấy giao dịch, chờ thêm {check_intervals[i]}s...")
    
    # Hết 5 lần vẫn không thấy → Cập nhật trạng thái FAILED
    print(f"❌ [{username}] Không tìm thấy giao dịch sau 10 phút")
    
    if update_deposit_order_status(order_id, "failed"):
        print(f"❌ [{username}] Đã cập nhật lệnh nạp #{order_id} → FAILED")
    else:
        print(f"⚠️ [{username}] Không cập nhật được trạng thái order")
    
    return False

def deposit(username: str, amount: int) -> dict:

    if not username or amount <= 0:
        return {"ok": False, "error": "Thiếu username hoặc amount không hợp lệ"}

    # Build params cho API nạp tiền
    params = {"amount": int(amount)}

    print(f"💰 [{username}] Đang tạo lệnh nạp {amount:,}đ...", flush=True)
    print(f"[DEBUG] Bắt đầu gọi game_request_with_retry", flush=True)
    try:
        resp = game_request_with_retry(username, "GET", DEPOSIT_URL, params=params, timeout=30)
        print(f"[DEBUG] Kết thúc gọi game_request_with_retry", flush=True)

        if not resp:
            print(f"❌ [{username}] Không nhận được response từ API", flush=True)
            return {"ok": False, "error": "Không gọi được API nạp tiền"}

        result = {"ok": resp.ok, "status": resp.status_code}
        try:
            result["data"] = resp.json()
        except Exception as e:
            print(f"⚠️ [{username}] Không parse được JSON: {e}", flush=True)
            result["text"] = resp.text

        print(f"[DEBUG] Đã xử lý xong response, chuẩn bị trả kết quả", flush=True)
        return result

    except Exception as e:
        print(f"❌ [{username}] Lỗi khi gọi deposit API: {e}")
        import traceback
        traceback.print_exc()
        return {"ok": False, "error": str(e)}

def save_deposit_to_db(username: str, api_result: dict, status: str = "pending") -> dict:
    """
    Lưu lệnh nạp tiền vào DB với trạng thái pending.
    
    Returns:
        dict: {ok: bool, orderId: int} - orderId để tracking sau này
    """
    payload = api_result.get("data", {}).get("data", {}) or {}
    rec = {
        "username": username,
        "accountNumber": payload.get("receiver", ""),
        "accountHolder": payload.get("name", ""),
        "transferContent": payload.get("msg", ""),
        "qrLink": payload.get("qr_link", ""),
        "qrPageUrl": payload.get("url", ""),
        # status = 'pending' mặc định ở server
    }
    try:
        r = requests.post(f"{NODE_SERVER_URL}/api/deposit-orders", json=rec, timeout=5)
        if r.status_code in (200, 201):
            data = r.json()
            return {"ok": True, "orderId": data.get("id")}
        return {"ok": False}
    except Exception as e:
        print(f"⚠️ Lỗi lưu DB: {e}")
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
            save_result = save_deposit_to_db(username, result)
            saved = save_result.get("ok")
            order_id = save_result.get("orderId")
            img_path = save_qr_image(payload, username)
            # In log đẹp với icon
            print()
            print(f"🎮 User: {username}", flush=True)
            print(f"👤 Tên TK: {payload.get('name', '')}", flush=True)
            print(f"🏦 Số TK: {payload.get('receiver', '')}", flush=True)
            print(f"💰 Số tiền: {amount:,} đ", flush=True)
            print(f"📝 Nội dung: \033[1;31m{payload.get('msg', '')}\033[0m", flush=True)
            print(f"[INFO] order_id (lưu DB): {order_id}", flush=True)
            print()
            # Trả kết quả JSON
            success_result = {
                "ok": True,
                "message": "Tạo lệnh nạp tiền thành công",
                "data": {
                    "username": username,
                    "amount": amount,
                    "accountNumber": payload.get('receiver', ''),
                    "accountHolder": payload.get('name', ''),
                    "transferContent": payload.get('msg', ''),
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
                transfer_content = payload.get('msg', '')
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
                save_result = save_deposit_to_db(u, result)
                saved = save_result.get("ok")
                order_id = save_result.get("orderId")
                
                img_path = save_qr_image(payload, u)
                if not img_path:
                    print("❌ Không lấy được ảnh QR (thiếu base64 và qr_link).", flush=True)
                else:
                    print("✅ Nạp thành công (đã lưu lệnh pending).", flush=True)
                    print(f"   Username: {u}", flush=True)
                    print(f"   STK nhận: {payload.get('receiver', '')}", flush=True)
                    print(f"   Tên: {payload.get('name', '')}", flush=True)
                    print(f"   NDCK: {payload.get('msg', '')}", flush=True)
                    print(f"   Ảnh QR: {img_path}", flush=True)
                    print(f"   Lưu DB: {'OK' if saved else 'Lỗi lưu'}", flush=True)
                    print(f"   [INFO] order_id (lưu DB): {order_id}", flush=True)
                    # Chờ và check lịch sử nạp tiền
                    if saved and order_id:
                        transfer_content = payload.get('msg', '')
                        wait_and_check_deposit(u, transfer_content, order_id, a)
