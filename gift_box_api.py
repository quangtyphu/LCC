import requests
import time
from datetime import datetime

# Dùng NODE_SERVER_URL chung
try:
    from fetch_transactions import NODE_SERVER_URL
except Exception:
    NODE_SERVER_URL = "http://127.0.0.1:3000"

GIFT_BOX_URL = "https://wlb.tele68.com/v1/lobby/gift-box"
CLAIM_GIFT_URL = "https://wlb.tele68.com/v1/lobby/gift-box/item"

def _build_proxies(proxy_str: str):
    # proxy format: host:port:user:pass
    host, port, userp, passp = proxy_str.split(":")
    proxy_auth = f"{userp}:{passp}@{host}:{port}"
    proxy_url = f"socks5h://{proxy_auth}"
    return {"http": proxy_url, "https": proxy_url}

def get_user_auth(username: str):
    r = requests.get(f"{NODE_SERVER_URL}/api/users/{username}", timeout=5)
    if r.status_code != 200:
        return None
    doc = r.json()
    proxy = doc.get("proxy")
    jwt = doc.get("jwt")
    access_token = doc.get("accessToken")
    if not proxy or not jwt or not access_token:
        return None
    return proxy, jwt, access_token

def fetch_gift_box(username: str) -> dict:
    """
    Gọi API gift-box với JWT + accessToken + proxy từ DB local.
    Trả về dict kết quả (ok/status/data hoặc error).
    """
    auth = get_user_auth(username)
    if not auth:
        return {"ok": False, "error": "Thiếu proxy/JWT/accessToken hoặc user không tồn tại"}
    proxy_str, jwt, access_token = auth

    # Parse proxy
    try:
        proxies = _build_proxies(proxy_str)
    except Exception:
        return {"ok": False, "error": "Proxy không hợp lệ"}

    params = {"cp": "R", "cl": "R", "pf": "web", "at": access_token}
    headers = {
        "accept": "*/*",
        "authorization": f"Bearer {jwt}",
        "content-type": "application/json",
        "origin": "https://play.lc79.bet",
        "referer": "https://play.lc79.bet/",
        "sec-ch-ua": "\"Google Chrome\";v=\"143\", \"Chromium\";v=\"143\", \"Not A(Brand\";v=\"24\"",
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": "\"Windows\"",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
    }

    try:
        resp = requests.get(GIFT_BOX_URL, params=params, headers=headers, proxies=proxies, timeout=20)
    except Exception as e:
        return {"ok": False, "error": f"Lỗi gọi API gift-box: {e}"}

    result = {"ok": resp.ok, "status": resp.status_code}
    try:
        result["data"] = resp.json()
    except Exception:
        result["text"] = resp.text
    return result

def claim_gift(username: str, gift_id: str) -> dict:
    """Nhận 1 quà theo ID"""
    auth = get_user_auth(username)
    if not auth:
        return {"ok": False, "error": "Thiếu proxy/JWT/accessToken"}
    proxy_str, jwt, access_token = auth

    # Parse proxy
    try:
        proxies = _build_proxies(proxy_str)
    except Exception:
        return {"ok": False, "error": "Proxy không hợp lệ"}

    params = {"cp": "R", "cl": "R", "pf": "web", "at": access_token}
    headers = {
        "accept": "*/*",
        "authorization": f"Bearer {jwt}",
        "content-type": "application/json",
        "origin": "https://play.lc79.bet",
        "referer": "https://play.lc79.bet/",
        "sec-ch-ua": "\"Google Chrome\";v=\"143\", \"Chromium\";v=\"143\", \"Not A(Brand\";v=\"24\"",
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": "\"Windows\"",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
    }
    payload = {"id": gift_id}

    try:
        resp = requests.post(CLAIM_GIFT_URL, params=params, headers=headers, json=payload, proxies=proxies, timeout=20)
    except Exception as e:
        return {"ok": False, "error": f"Lỗi nhận quà: {e}"}

    result = {"ok": resp.ok, "status": resp.status_code}
    try:
        result["data"] = resp.json()
    except Exception:
        result["text"] = resp.text
    return result

def update_user_balance(username: str, new_balance: float) -> bool:
    """Cập nhật số dư mới vào DB local qua API PUT /api/users/:username"""
    try:
        r = requests.put(
            f"{NODE_SERVER_URL}/api/users/{username}",
            json={"balance": new_balance},
            timeout=5
        )
        return r.status_code == 200
    except Exception as e:
        print(f"   ⚠️ Lỗi cập nhật balance: {e}")
        return False

def auto_claim_gifts(username: str):
    """
    Check danh sách quà, nếu có quà chưa nhận → nhận từng cái, cách nhau 3s.
    Chỉ log khi có quà được nhận thành công.
    """
    result = fetch_gift_box(username)

    if not result.get("ok"):
        return

    data = result.get("data")
    if not isinstance(data, list) or not data:
        return

    # Lọc quà chưa nhận
    unclaimed = [g for g in data if not g.get("isClaim", False)]
    if not unclaimed:
        return

    # Nhận từng quà
    for i, g in enumerate(unclaimed, 1):
        gift_id = g.get("id", "")
        title = g.get("title", "")
        amount = g.get("bonusAmount", 0)
        created = g.get("createTime", "")
        
        claim_result = claim_gift(username, gift_id)
        if claim_result.get("ok"):
            claim_data = claim_result.get("data", {})
            
            if isinstance(claim_data, dict):
                balance = claim_data.get("balance")
                
                # Chỉ log khi nhận thành công
                if balance is not None:
                    print(f"🎁 [{username}] {created} | Nhận: {title} (+{amount:,}đ) → Số dư: {balance:,}đ")
                    update_user_balance(username, float(balance))

        # Delay 3s
        if i < len(unclaimed):
            time.sleep(3)