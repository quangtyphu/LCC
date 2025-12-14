import os, re, base64, requests
from datetime import datetime

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

def deposit(username: str, amount: int) -> dict:

    if not username or amount <= 0:
        return {"ok": False, "error": "Thiếu username hoặc amount không hợp lệ"}

    # 1) Lấy thông tin user
    try:
        ur = requests.get(f"{NODE_SERVER_URL}/api/users/{username}", timeout=5)
        if ur.status_code != 200:
            return {"ok": False, "error": "Không tìm thấy user"}
        udoc = ur.json()
    except Exception as e:
        return {"ok": False, "error": f"Lỗi lấy user: {e}"}

    proxy_str = udoc.get("proxy")
    jwt = udoc.get("jwt")
    access_token = udoc.get("accessToken")
    if not proxy_str or not jwt or not access_token:
        return {"ok": False, "error": "Thiếu proxy/JWT/accessToken"}

    # 2) Parse proxy
    try:
        host, port, up, pw = proxy_str.split(":")
        proxy_url = f"socks5h://{up}:{pw}@{host}:{port}"
        proxies = {"http": proxy_url, "https": proxy_url}
    except Exception:
        return {"ok": False, "error": "Proxy không hợp lệ"}

    # 3) Gọi API nạp tiền
    params = {
        "amount": int(amount),
        "cp": "R",
        "cl": "R",
        "pf": "web",
        "at": access_token,
    }
    headers = {
        "Authorization": f"Bearer {jwt}",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        "Referer": "https://play.lc79.bet/",
        "sec-ch-ua": "\"Google Chrome\";v=\"143\", \"Chromium\";v=\"143\", \"Not A(Brand\";v=\"24\"",
        "sec-ch-ua-platform": "\"Windows\"",
        "sec-ch-ua-mobile": "?0",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.get(DEPOSIT_URL, params=params, headers=headers, proxies=proxies, timeout=20)
    except Exception as e:
        return {"ok": False, "error": f"Lỗi gọi API nạp: {e}"}

    result = {"ok": resp.ok, "status": resp.status_code}
    try:
        result["data"] = resp.json()
    except Exception:
        result["text"] = resp.text

    return result

def save_deposit_to_db(username: str, api_result: dict, status: str = "pending") -> bool:
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
        return r.status_code in (200, 201)
    except Exception as e:
        print(f"⚠️ Lỗi lưu DB: {e}")
        return False

# Ví dụ chạy nhanh trong terminal Python:
if __name__ == "__main__":
    u = input("Username: ").strip()
    a = int(input("Amount: ").strip() or "0")
    result = deposit(u, a)

    if not result.get("ok"):
        print(f"❌ Lỗi: {result.get('error', 'Unknown error')}")
    else:
        payload = result.get("data", {}).get("data", {}) or {}
        if not payload:
            api_error = result.get("data", {}).get("message", "API không trả dữ liệu")
            api_code = result.get("data", {}).get("code", "?")
            print(f"❌ Lỗi API: [{api_code}] {api_error}")
        else:
            saved = save_deposit_to_db(u, result)
            img_path = save_qr_image(payload, u)
            if not img_path:
                print("❌ Không lấy được ảnh QR (thiếu base64 và qr_link).")
            else:
                print("✅ Nạp thành công (đã lưu lệnh pending).")
                print(f"   Username: {u}")
                print(f"   STK nhận: {payload.get('receiver', '')}")
                print(f"   Tên: {payload.get('name', '')}")
                print(f"   NDCK: {payload.get('msg', '')}")
                print(f"   Ảnh QR: {img_path}")
                print(f"   Lưu DB: {'OK' if saved else 'Lỗi lưu'}")
