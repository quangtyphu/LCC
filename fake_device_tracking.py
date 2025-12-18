"""
Fake API tracking device (v1/trk/dv) cho game
- Gửi uuid, appId, proxy, token, headers giống web
- Tham khảo các file game_api_helper.py, jwt_manager.py, ...
"""
import sys
import uuid as uuidlib
from curl_cffi import requests
from game_api_helper import build_proxies, build_common_headers, get_user_auth

TRACK_URL = "https://gameapi.tele68.com/v1/trk/dv"
APP_ID = "https://play.lc79.bet"

def fake_device_tracking(username: str, uuid: str = None):
    # Lấy auth info (proxy, jwt, access_token, nickname)
    auth = get_user_auth(username)
    if not auth:
        print(f"❌ Không lấy được auth info cho {username}")
        return
    proxy_str, jwt, access_token, _ = auth
    proxies = build_proxies(proxy_str)
    headers = build_common_headers(jwt)
    params = {
        "cp": "R",
        "cl": "R",
        "pf": "web",
        "at": access_token
    }

    # Lấy uuid từ DB nếu có, nếu chưa có thì random và cập nhật vào DB
    if not uuid:
        import requests as pyrequests
        API_BASE = "http://127.0.0.1:3000"
        try:
            resp = pyrequests.get(f"{API_BASE}/api/accounts/{username}", timeout=5)
            if resp.status_code == 200:
                user = resp.json()
                uuid = user.get("uuid")
                if not uuid:
                    uuid = str(uuidlib.uuid4())
                    # Cập nhật uuid vào DB
                    update_resp = pyrequests.put(
                        f"{API_BASE}/api/accounts/{username}",
                        json={"uuid": uuid},
                        timeout=5
                    )
                    if update_resp.status_code == 200:
                        print(f"[DEBUG] Đã random và cập nhật uuid mới vào DB: {uuid}")
                    else:
                        print(f"[WARN] Không cập nhật được uuid vào DB: {update_resp.text}")
                else:
                    print(f"[DEBUG] Lấy uuid từ DB: {uuid}")
            else:
                print(f"[WARN] Không lấy được uuid từ DB, random tạm thời")
                uuid = str(uuidlib.uuid4())
        except Exception as e:
            print(f"[WARN] Lỗi khi lấy/cập nhật uuid từ DB: {e}")
            uuid = str(uuidlib.uuid4())

    data = {
        "uuid": uuid,
        "appId": APP_ID
    }
    print(f"\n[DEBUG] Gửi tracking device với uuid: {uuid}")
    try:
        resp = requests.post(
            TRACK_URL,
            params=params,
            headers=headers,
            json=data,
            proxies=proxies,
            timeout=15,
            impersonate="chrome120"
        )
        print(f"[DEBUG] Status: {resp.status_code}")
        print(f"[DEBUG] Response: {resp.text}")
    except Exception as e:
        print(f"❌ Lỗi gửi tracking: {e}")

if __name__ == "__main__":
    username = input("👤 Nhập username: ").strip()
    uuid = input("🔑 Nhập uuid (Enter để random): ").strip() or None
    fake_device_tracking(username, uuid)
