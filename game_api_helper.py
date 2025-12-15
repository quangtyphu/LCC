"""
Game API Helper - Utilities chung cho các API gọi game
Chứa các hàm tái sử dụng: proxy, auth, request wrapper
"""
import sys
import io

# Fix encoding cho Windows console
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from curl_cffi import requests

NODE_SERVER_URL = "http://127.0.0.1:3000"

# Import jwt_manager để refresh token
try:
    from jwt_manager import refresh_jwt_and_token
except ImportError:
    def refresh_jwt_and_token(username: str) -> bool:
        print(f"WARNING: Không tìm thấy jwt_manager.py", flush=True)
        return False


def build_proxies(proxy_str: str) -> dict | None:
    """
    Parse proxy string thành dict cho requests.
    
    Args:
        proxy_str: Format "host:port:user:pass"
    
    Returns:
        {"http": "socks5h://...", "https": "socks5h://..."}
    """
    if not proxy_str:
        return None
    try:
        host, port, userp, passp = proxy_str.split(":")
        proxy_auth = f"{userp}:{passp}@{host}:{port}"
        proxy_url = f"socks5h://{proxy_auth}"
        return {"http": proxy_url, "https": proxy_url}
    except Exception:
        return None


def get_user_auth(username: str) -> tuple | None:
    """
    Lấy thông tin auth từ DB local.
    
    Args:
        username: Username trong DB
    
    Returns:
        (proxy_str, jwt, access_token, nickname) hoặc None nếu lỗi
    """
    try:
        resp = requests.get(f"{NODE_SERVER_URL}/api/users/{username}", timeout=5)
        if resp.status_code != 200:
            return None
        
        user = resp.json()
        proxy_str = user.get("proxy")
        jwt = user.get("jwt")
        access_token = user.get("accessToken")
        nickname = user.get("nickname", "")
        
        if not proxy_str or not jwt or not access_token:
            return None
        
        return (proxy_str, jwt, access_token, nickname)
    
    except Exception:
        return None


def build_common_headers(jwt: str, user_agent: str = None) -> dict:
    """
    Tạo headers chuẩn cho API game.
    
    Args:
        jwt: JWT token
        user_agent: Custom User-Agent (optional)
    
    Returns:
        dict headers
    """
    if not user_agent:
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
    
    return {
        "accept": "*/*",
        "accept-language": "vi-VN,vi;q=0.9",
        "authorization": f"Bearer {jwt}",
        "content-type": "application/json",
        "origin": "https://play.lc79.bet",
        "referer": "https://play.lc79.bet/",
        "user-agent": user_agent,
        "sec-ch-ua": '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-mobile": "?0",
    }


def build_common_params(access_token: str) -> dict:
    """
    Tạo query params chuẩn cho API game.
    
    Args:
        access_token: Access token
    
    Returns:
        dict params
    """
    return {
        "cp": "R",
        "cl": "R",
        "pf": "web",
        "at": access_token
    }


def game_request_with_retry(
    username: str,
    method: str,
    url: str,
    params: dict = None,
    json_data: dict = None,
    timeout: int = 20
) -> requests.Response | None:
    """
    Gọi API game với auto-retry khi token hết hạn (401/403).
    
    Args:
        username: Username để lấy auth
        method: "GET", "POST", hoặc "PUT"
        url: URL đầy đủ của API
        params: Query params (sẽ merge với common params)
        json_data: Body JSON (cho POST/PUT)
        timeout: Timeout seconds
    
    Returns:
        Response object hoặc None nếu lỗi
    """
    # 1. Lấy auth info
    auth = get_user_auth(username)
    if not auth:
        print(f"❌ [{username}] Không lấy được auth info", flush=True)
        return None
    
    proxy_str, jwt, access_token, _ = auth
    
    # 2. Setup proxy
    proxies = build_proxies(proxy_str)
    if not proxies:
        print(f"❌ [{username}] Proxy không hợp lệ", flush=True)
        return None
    
    # 3. Build headers & params
    headers = build_common_headers(jwt)
    common_params = build_common_params(access_token)
    
    # Merge params
    if params:
        common_params.update(params)
    
    # 4. Gọi API
    try:
        if method.upper() == "GET":
            resp = requests.get(
                url,
                params=common_params,
                headers=headers,
                proxies=proxies,
                timeout=timeout,
                impersonate="chrome120"
            )
        elif method.upper() == "POST":
            resp = requests.post(
                url,
                params=common_params,
                headers=headers,
                json=json_data,
                proxies=proxies,
                timeout=timeout,
                impersonate="chrome120"
            )
        elif method.upper() == "PUT":
            resp = requests.put(
                url,
                params=common_params,
                headers=headers,
                json=json_data,
                proxies=proxies,
                timeout=timeout,
                impersonate="chrome120"
            )
        else:
            print(f"❌ Method không hợp lệ: {method}", flush=True)
            return None
    except Exception as e:
        print(f"❌ [{username}] Lỗi request: {e}", flush=True)
        return None
    
    # 5. Auto-retry nếu 401/403
    if resp.status_code in (401, 403):
        print(f"⚠️ [{username}] Token hết hạn, đang refresh...", flush=True)
        
        if refresh_jwt_and_token(username):
            # Lấy lại token mới
            auth2 = get_user_auth(username)
            if auth2:
                _, jwt2, access_token2, _ = auth2
                headers["authorization"] = f"Bearer {jwt2}"
                common_params["at"] = access_token2
                
                # Retry
                try:
                    if method.upper() == "GET":
                        resp = requests.get(
                            url,
                            params=common_params,
                            headers=headers,
                            proxies=proxies,
                            timeout=timeout,
                            impersonate="chrome120"
                        )
                    elif method.upper() == "POST":
                        resp = requests.post(
                            url,
                            params=common_params,
                            headers=headers,
                            json=json_data,
                            proxies=proxies,
                            timeout=timeout,
                            impersonate="chrome120"
                        )
                    elif method.upper() == "PUT":
                        resp = requests.put(
                            url,
                            params=common_params,
                            headers=headers,
                            json=json_data,
                            proxies=proxies,
                            timeout=timeout,
                            impersonate="chrome120"
                        )
                    print(f"✅ [{username}] Đã refresh token và retry", flush=True)
                except Exception as e:
                    print(f"❌ [{username}] Lỗi retry: {e}", flush=True)
                    return None
        else:
            print(f"❌ [{username}] Không refresh được token", flush=True)
            return None
    
    return resp


def update_user_balance(username: str, new_balance: float) -> bool:
    """
    Cập nhật balance vào DB local.
    
    Args:
        username: Username
        new_balance: Balance mới
    
    Returns:
        True nếu thành công
    """
    try:
        resp = requests.put(
            f"{NODE_SERVER_URL}/api/users/{username}",
            json={"balance": new_balance},
            timeout=5
        )
        return resp.status_code == 200
    except Exception as e:
        print(f"⚠️ [{username}] Lỗi cập nhật balance: {e}", flush=True)
        return False
