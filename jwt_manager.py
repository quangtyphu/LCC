# jwt_manager.py
# Nhiệm vụ:
# - Login qua proxy bằng nickname + accessToken
# - Lấy balance ngay tại bước login (remoteLoginResp.money | money) và cập nhật vào API
# - Tùy chọn cập nhật JWT mới (update_jwt=True) hoặc KHÔNG cập nhật (update_jwt=False) để an toàn WS
# - (Tuỳ chọn) Fetch lịch sử nạp/rút sau login

import requests
import random
import time
from fetch_transactions import fetch_transactions

API_BASE = "http://127.0.0.1:3000"  # URL server.js
LOGIN_URL = "https://wlb.tele68.com/v1/lobby/auth/login?cp=R&cl=R&pf=web&at="

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) Chrome/118.0.5993.88 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) Chrome/122.0.6261.57 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0",
]


def refresh_jwt(
    user_name: str,
    *,
    update_jwt: bool = True,        # True: ghi JWT mới vào DB; False: chỉ login để lấy balance (an toàn WS)
    update_balance: bool = True,    # True: cập nhật balance từ response login
    fetch_tx: bool = True           # True: fetch DEPOSIT rồi WITHDRAW (có delay 15s)
) -> str | None:
    """
    Login qua proxy để lấy JWT *và* balance ngay tại bước login.
    - update_jwt=True  : hành vi refresh thật sự (ghi JWT mới).
    - update_jwt=False : KHÔNG ghi JWT (chỉ kéo balance), tránh ảnh hưởng WS đang sống.
    - update_balance   : có cập nhật balance từ (remoteLoginResp.money | money) hay không.
    - fetch_tx         : có gọi fetch_transactions (DEPOSIT rồi WITHDRAW) sau login hay không.

    Trả về:
        - JWT string nếu update_jwt=True và server trả token
        - None nếu update_jwt=False (vì bạn chỉ kéo balance) hoặc login lỗi.
    """

    # 1) Lấy thông tin user (nickname, accessToken, proxy)
    try:
        resp = requests.get(f"{API_BASE}/api/users/{user_name}", timeout=7)
        if resp.status_code != 200:
            print(f"❌ [{user_name}] Không lấy được user (API {resp.status_code})")
            return None
        acc = resp.json()
    except Exception as e:
        print(f"❌ [{user_name}] Lỗi gọi API users: {e}")
        return None

    nick = acc.get("nickname")
    token = acc.get("accessToken")
    proxy = acc.get("proxy")

    if not nick or not token:
        print(f"⚠️ [{user_name}] Thiếu nickname/accessToken trong DB")
        _update_status(user_name, "Token Lỗi")
        return None
    if not proxy:
        print(f"⚠️ [{user_name}] Không có proxy trong DB")
        _update_status(user_name, "Proxy Lỗi")
        return None

    # 2) Chuẩn bị proxy cho requests
    try:
        host, port, userp, passp = proxy.split(":")
        proxy_auth = f"{userp}:{passp}@{host}:{port}"
        proxy_url = f"socks5h://{proxy_auth}"
        proxies = {"http": proxy_url, "https": proxy_url}
    except Exception:
        print(f"⚠️ [{user_name}] Proxy sai định dạng: {proxy}")
        _update_status(user_name, "Proxy Lỗi")
        return None

    headers = {
        "content-type": "application/json",
        "origin": "https://play.lc79.bet",
        "referer": "https://play.lc79.bet/",
        "user-agent": random.choice(USER_AGENTS),
    }
    payload = {"nickName": nick, "accessToken": token}

    # 3) Gọi login để lấy JWT + balance
    try:
        print(f"🔐 [{user_name}] Login qua proxy...")
        r = requests.post(LOGIN_URL, json=payload, headers=headers, proxies=proxies, timeout=25)
    except Exception as e:
        print(f"❌ [{user_name}] Lỗi login: {e}")
        _update_status(user_name, "Proxy Lỗi")
        return None

    if r.status_code == 401:
        print(f"❌ [{user_name}] Login 401 Unauthorized → Token Lỗi")
        _update_status(user_name, "Token Lỗi")
        return None
    if r.status_code != 200:
        print(f"❌ [{user_name}] Login lỗi {r.status_code}: {r.text[:200]}")
        _update_status(user_name, "Proxy Lỗi")
        return None

    try:
        data = r.json()
    except Exception:
        print(f"⚠️ [{user_name}] Response login không phải JSON")
        return None

    # 4) Lấy JWT và Balance ngay tại bước login
    jwt = data.get("token")
    money = (data.get("remoteLoginResp") or {}).get("money") or data.get("money")

    # 4.1) Cập nhật balance nếu có
    if update_balance and money is not None:
        try:
            ub = requests.put(f"{API_BASE}/api/users/{user_name}", json={"balance": money}, timeout=7)
            if ub.status_code == 200:
                print(f"💰 [{user_name}] Balance cập nhật từ login = {money}")
            else:
                print(f"⚠️ [{user_name}] Update balance lỗi: {ub.status_code} {ub.text[:120]}")
        except Exception as e:
            print(f"⚠️ [{user_name}] Update balance exception: {e}")

    # 4.2) Ghi JWT mới nếu được phép
    if update_jwt:
        if not jwt:
            print(f"⚠️ [{user_name}] Login không trả về token → không thể cập nhật JWT")
            return None
        try:
            uj = requests.put(f"{API_BASE}/api/users/{user_name}", json={"jwt": jwt}, timeout=7)
            if uj.status_code == 200:
                print(f"🔑 [{user_name}] JWT đã cập nhật từ login")
            else:
                print(f"⚠️ [{user_name}] Update JWT lỗi: {uj.status_code} {uj.text[:120]}")
        except Exception as e:
            print(f"⚠️ [{user_name}] Update JWT exception: {e}")

    # 5) (Tuỳ chọn) Fetch lịch sử nạp/rút
    if fetch_tx:
        try:
            try:
                fetch_transactions(user_name, tx_type="DEPOSIT", limit=10)
            except Exception as e:
                print(f"⚠️ [{user_name}] Fetch DEPOSIT lỗi: {e}")
            time.sleep(35)
            try:
                fetch_transactions(user_name, tx_type="WITHDRAW", limit=10)
            except Exception as e:
                print(f"⚠️ [{user_name}] Fetch WITHDRAW lỗi: {e}")
        except Exception:
            # Không có module hoặc bạn không muốn dùng -> bỏ qua yên lặng
            pass

    # Trả về JWT mới nếu update_jwt=True, ngược lại None (vì bạn chỉ kéo balance)
    return jwt if update_jwt else None


# ------------------- Helper -------------------
def _update_status(user: str, status: str):
    try:
        r = requests.put(f"{API_BASE}/api/users/{user}", json={"status": status}, timeout=5)
        if r.status_code == 200:
            print(f"💾 [{user}] Status cập nhật = {status}")
    except Exception as e:
        print(f"⚠️ [{user}] Không gọi API update status được: {e}")


# ------------------- Tiện ích: login chỉ để lấy balance (không ghi JWT) -------------------
def login_for_balance(user_name: str) -> None:
    """
    Trường hợp muốn thay hẳn your-info:
    Gọi hàm này để login và cập nhật balance ngay, KHÔNG ghi JWT, KHÔNG fetch tx.
    """
    refresh_jwt(user_name, update_jwt=False, update_balance=True, fetch_tx=False)


if __name__ == "__main__":
    uname = input("👤 Nhập username: ").strip()
    # Mặc định: refresh thật sự (ghi JWT + balance + fetch tx)
    new_jwt = refresh_jwt(uname, update_jwt=True, update_balance=True, fetch_tx=True)
    if new_jwt:
        print(f"👉 JWT mới: {new_jwt[:30]}...{new_jwt[-30:]}")
