import requests
import hashlib

GATEWAY_URL = "https://apifo88daigia.tele68.com/api"
NODE_SERVER_URL = "http://127.0.0.1:3000"  # API Node.js local

def _build_proxies(proxy_str: str | None):
    if not proxy_str:
        return None
    host, port, userp, passp = proxy_str.split(":")
    proxy_auth = f"{userp}:{passp}@{host}:{port}"
    proxy_url = f"socks5h://{proxy_auth}"
    return {"http": proxy_url, "https": proxy_url}


def _fetch_proxy_from_db(username: str) -> str | None:
    try:
        r = requests.get(f"{NODE_SERVER_URL}/api/users/{username}", timeout=5)
        if r.status_code != 200:
            return None
        p = r.json().get("proxy")
        if p is not None and str(p).strip():
            return str(p).strip()
    except Exception:
        pass
    return None


def get_access_token(username: str, password: str, proxy_str: str | None = None) -> str | None:
    """
    Lấy accessToken từ gateway bằng username + password.
    Luôn gọi gateway qua SOCKS; nếu không truyền proxy thì đọc từ DB user.
    """
    p = (proxy_str or "").strip()
    if not p:
        p = _fetch_proxy_from_db(username) or ""
    p = p.strip()
    if not p:
        return None
    try:
        proxies = _build_proxies(p)
    except Exception:
        return None
    if not proxies:
        return None
    params = {
        "c": "3",
        "un": username,
        "pw": hashlib.md5(password.encode()).hexdigest(),
        "cp": "R",
        "cl": "R",
        "pf": "web",
        "at": ""
    }
    headers = {
        "accept": "*/*",
        "origin": "https://play.lc79.bet",
        "referer": "https://play.lc79.bet/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
    }
    
    try:
        r = requests.get(GATEWAY_URL, params=params, headers=headers, proxies=proxies, timeout=15)
        if not r.ok:
            return None
        
        data = r.json()
        
        # Parse accessToken từ response
        if isinstance(data, dict):
            if "data" in data and isinstance(data["data"], dict):
                return data["data"].get("accessToken")
            if "accessToken" in data:
                return data["accessToken"]
            if isinstance(data.get("data"), list) and data["data"]:
                for item in data["data"]:
                    if isinstance(item, dict) and "accessToken" in item:
                        return item["accessToken"]
        return None
    except Exception:
        return None

def update_access_token_to_db(username: str, access_token: str) -> bool:
    """
    Cập nhật accessToken vào DB qua API Node.js
    """
    try:
        r = requests.post(
            f"{NODE_SERVER_URL}/api/users/accessToken",
            json={"username": username, "accessToken": access_token},
            timeout=5
        )
        return r.status_code == 200 and r.json().get("ok")
    except Exception as e:
        print(f"⚠️ Lỗi cập nhật DB: {e}")
        return False

if __name__ == "__main__":
    username = input("Username: ").strip()
    password = input("Password: ").strip()
    proxy = input("Proxy (host:port:user:pass, Enter = lấy từ DB): ").strip() or None

    token = get_access_token(username, password, proxy)
    if token:
        print(f"✅ accessToken: {token}")
        
        # Tự động cập nhật vào DB
        if update_access_token_to_db(username, token):
            print(f"💾 Đã cập nhật accessToken vào DB cho {username}")
        else:
            print(f"⚠️ Không cập nhật được DB (kiểm tra server Node.js)")
    else:
        print("❌ Không lấy được accessToken")