# jwt_manager.py
# Nhiệm vụ:
# - Login qua proxy bằng nickname + accessToken
# - Lấy balance ngay tại bước login (remoteLoginResp.money | money) và cập nhật vào API
# - Tùy chọn cập nhật JWT mới (update_jwt=True) hoặc KHÔNG cập nhật (update_jwt=False) để an toàn WS
# - (Tuỳ chọn) Fetch lịch sử nạp/rút sau login

from curl_cffi import requests as curl_requests
import requests as std_requests  # Fallback khi curl_cffi lỗi TLS
import time

API_BASE = "http://127.0.0.1:3000"  # URL server.js
LOGIN_URL = "https://wlb.tele68.com/v1/lobby/auth/login"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) Chrome/118.0.5993.88 Safari/537.36",
]


def _build_proxies(proxy_str: str):
    """Tạo dict proxies cho requests"""
    host, port, userp, passp = proxy_str.split(":")
    proxy_auth = f"{userp}:{passp}@{host}:{port}"
    proxy_url = f"socks5h://{proxy_auth}"
    return {"http": proxy_url, "https": proxy_url}


def refresh_jwt(username: str, _retry_count: int = 0) -> str | None:
    """
    Lấy JWT mới bằng cách login lại với accessToken.
    Nếu 401 → tự động lấy accessToken mới và retry (max 2 lần).
    """
    from game_login import get_access_token, update_access_token_to_db
    
    MAX_RETRY = 2
    if _retry_count >= MAX_RETRY:
        print(f"❌ [{username}] Đã retry {MAX_RETRY} lần, dừng lại")
        return None
    
    try:
        # 1. Lấy user_profile (có nickname, proxy, accessToken, jwt)
        resp_profile = curl_requests.get(f"{API_BASE}/api/users/{username}", timeout=5)
        if resp_profile.status_code != 200:
            print(f"❌ [{username}] Không lấy được user_profile từ DB")
            return None
        
        profile = resp_profile.json()
        proxy_str = profile.get("proxy")
        access_token = profile.get("accessToken")
        nickname = profile.get("nickname") or username  # Lấy nickname từ user_profiles
        
        if not access_token:
            print(f"⚠️ [{username}] Không có accessToken trong DB")
            return None
        
        # 2. Setup proxy
        try:
            proxies = _build_proxies(proxy_str)
        except Exception:
            print(f"⚠️ [{username}] Proxy lỗi format")
            return None
        
        # 3. Login (bỏ log)
        
        params = {"cp": "R", "cl": "R", "pf": "web", "at": access_token}
        headers = {
            "accept": "*/*",
            "authorization": "Bearer null",
            "content-type": "application/json",
            "origin": "https://play.lc79.bet",
            "referer": "https://play.lc79.bet/",
            "user-agent": USER_AGENTS[0],
        }
        payload = {"nickName": nickname, "accessToken": access_token}
        
        # Thử curl_cffi trước; nếu lỗi TLS (35) thì fallback sang requests chuẩn
        r = None
        try:
            r = curl_requests.post(
                LOGIN_URL,
                params=params,
                headers=headers,
                json=payload,
                proxies=proxies,
                timeout=20,
                impersonate="chrome120"
            )
        except Exception as e:
            err_msg = str(e).lower()
            # TLS (35), timeout (28), connection closed (56), HTTP2 framing (16) → fallback requests
            need_fallback = (
                "curl: (35)" in err_msg or "boringssl" in err_msg or "invalid library" in err_msg or "ssl_error_syscall" in err_msg
                or "curl: (28)" in err_msg or "curl: (56)" in err_msg or "curl: (16)" in err_msg
            )
            if need_fallback:
                try:
                    print(f"⚠️ [{username}] curl_cffi lỗi → thử requests chuẩn...", flush=True)
                    # Timeout (28) → dùng timeout dài hơn cho fallback
                    fb_timeout = 35 if "curl: (28)" in err_msg else 20
                    r = std_requests.post(
                        LOGIN_URL,
                        params=params,
                        headers=headers,
                        json=payload,
                        proxies=proxies,
                        timeout=fb_timeout
                    )
                except Exception as e2:
                    print(f"❌ [{username}] Fallback requests cũng lỗi: {e2}", flush=True)
                    raise e
            else:
                raise
        
        if r is None:
            return None
        
        # === Xử lý 401 ===
        if r.status_code == 401:
            
            # Lấy password từ bảng accounts
            resp_acc = curl_requests.get(f"{API_BASE}/api/accounts/{username}", timeout=5)
            if resp_acc.status_code != 200:
                print(f"❌ [{username}] Không lấy được account từ DB")
                return None
            
            account = resp_acc.json()
            password = account.get("loginPass")
            if not password:
                print(f"❌ [{username}] Không có loginPass trong accounts")
                return None

            # Bỏ log lấy accessToken mới
            old_token = access_token
            new_access_token = get_access_token(username, password, proxy_str)
            
            if not new_access_token:
                print(f"❌ [{username}] Gateway không trả về accessToken")
                return None
            
            # Kiểm tra token mới khác token cũ
            if new_access_token == old_token:
                print(f"❌ [{username}] Gateway trả về token cũ → username/password SAI hoặc account bị KHÓA!")
                print(f"   👉 Kiểm tra lại loginPass trong accounts: {password}")
                return None
            
            # Cập nhật DB
            if not update_access_token_to_db(username, new_access_token):
                print(f"⚠️ [{username}] Không cập nhật được accessToken vào DB")
                return None
            
            # Đợi 1s rồi retry
            time.sleep(1)
            # Bỏ log retry login
            return refresh_jwt(username, _retry_count + 1)
        
        # === Xử lý response khác ===
        if not r.ok:
            print(f"❌ [{username}] Login {r.status_code} {r.reason}")
            try:
                err_data = r.json()
                print(f"📄 [{username}] Error: {err_data.get('message', r.text[:150])}")
            except:
                print(f"📄 [{username}] Response: {r.text[:150]}")
            return None
        
        # === Parse JWT ===
        data = r.json()
        
        # Response format: {"token": "jwt...", "remoteLoginResp": {"money": 123, "code": 0}}
        jwt_token = data.get("token")
        remote_resp = data.get("remoteLoginResp", {})
        
        if jwt_token and remote_resp.get("code") == 0:
            # Cập nhật balance
            balance = remote_resp.get("money", 0)
            print(f"   💰 Balance: {balance:,}đ")
            
            try:
                curl_requests.put(
                    f"{API_BASE}/api/users/{username}",
                    json={"balance": balance},
                    timeout=5
                )
                print(f"💾 [{username}] Đã cập nhật balance: {balance:,}đ")
            except Exception as e:
                print(f"⚠️ [{username}] Không cập nhật được balance: {e}")
            
            return jwt_token
        
        # Xử lý lỗi
        print(f"❌ [{username}] Login thất bại")
        print(f"📄 [{username}] Response: {data}")
        return None
        
    except Exception as e:
        print(f"❌ [{username}] Lỗi refresh JWT: {e}")
        import traceback
        traceback.print_exc()
        return None


def _update_status(user: str, status: str):
    try:
        r = curl_requests.put(f"{API_BASE}/api/users/{user}", json={"status": status}, timeout=5)
        if r.status_code == 200:
            print(f"💾 [{user}] Status cập nhật = {status}")
    except Exception as e:
        print(f"⚠️ [{user}] Không gọi API update status được: {e}")


def refresh_jwt_and_token(username: str) -> bool:
    """
    Wrapper function được gọi từ deposit_api.py và withdraw.py
    Refresh JWT và accessToken, cập nhật vào DB.
    
    Returns:
        True nếu refresh thành công, False nếu thất bại
    """
    try:
        new_jwt = refresh_jwt(username)
        if new_jwt:
            # Cập nhật JWT vào DB
            try:
                resp = curl_requests.put(
                    f"{API_BASE}/api/users/{username}",
                    json={"jwt": new_jwt},
                    timeout=5
                )
                if resp.status_code == 200:
                    return True
                else:
                    print(f"⚠️ [{username}] Không cập nhật được JWT vào DB")
                    return False
            except Exception as e:
                print(f"⚠️ [{username}] Lỗi cập nhật JWT: {e}")
                return False
        else:
            return False
    except Exception as e:
        print(f"❌ [{username}] Lỗi refresh_jwt_and_token: {e}")
        return False


# ------------------- Tiện ích: login chỉ để lấy balance (không ghi JWT) -------------------
def login_for_balance(user_name: str) -> None:
    """
    Trường hợp muốn thay hẳn your-info:
    Gọi hàm này để login và cập nhật balance ngay, KHÔNG ghi JWT, KHÔNG fetch tx.
    """
    refresh_jwt(user_name)


if __name__ == "__main__":
    uname = input("👤 Nhập username: ").strip()
    new_jwt = refresh_jwt(uname)
    if new_jwt:
        print(f"👉 JWT: {new_jwt[:30]}...{new_jwt[-30:]}")
