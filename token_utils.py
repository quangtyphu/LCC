# Kiểm tra & refresh token
import asyncio, json, requests
import socks, websockets
from constants import WS_URL

API_BASE = "http://127.0.0.1:3000"  # URL server.js của bạn


def _fetch_proxy_str(username: str) -> str | None:
    try:
        r = requests.get(f"{API_BASE}/api/users/{username}", timeout=5)
        if r.status_code != 200:
            return None
        p = r.json().get("proxy")
        if p is not None and str(p).strip():
            return str(p).strip()
    except Exception:
        pass
    return None


# ------------------- Cập nhật trạng thái user qua API -------------------
def update_user_status(user, status):
    try:
        r = requests.put(f"{API_BASE}/api/users/{user}", json={"status": status}, timeout=3)
        if r.status_code == 200:
            print(f"💾 [{user}] Cập nhật status = {status}")
        else:
            print(f"⚠️ [{user}] Lỗi update status API: {r.text}")
    except Exception as e:
        print(f"⚠️ [{user}] Không kết nối được API khi update status: {e}")


# ------------------- Test token còn xài được hay không -------------------
async def test_token(jwt, proxy_str=None, user=None):
    """
    Trả về True nếu token hợp lệ, False nếu không.
    Bắt buộc SOCKS proxy (tham số hoặc lấy từ DB khi có user); không kết nối WS trực tiếp.
    """
    proxy = (proxy_str or "").strip()
    if not proxy and user:
        proxy = _fetch_proxy_str(user) or ""
    proxy = proxy.strip()
    if not proxy:
        if user:
            print(
                f"⚠️ [{user}] test_token: thiếu proxy, không mở WS tới game (chỉ qua SOCKS)",
                flush=True,
            )
        return False

    ws = None
    ok = False
    try:
        host, port, puser, ppass = proxy.split(":")
        port = int(port)
        sock = socks.socksocket()
        sock.set_proxy(socks.SOCKS5, host, port, True, puser, ppass)
        sock.settimeout(10)
        sock.connect(("wtx.tele68.com", 443))
        ws = await websockets.connect(WS_URL, sock=sock, ssl=True, ping_interval=None)

        await ws.recv()  # bỏ handshake
        await ws.send(f"40/tx,{json.dumps({'token': jwt})}")

        for _ in range(10):
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=3)
                if msg.startswith("42/tx,"):
                    arr = json.loads(msg[len("42/tx,"):])
                    if arr[0] == "your-info":
                        ok = True
                        break
            except asyncio.TimeoutError:
                break
    except Exception:
        ok = False
    finally:
        if ws:
            await ws.close()

    # Đồng bộ trạng thái user nếu có
    if user:
        if ok:
            update_user_status(user, "Đang Chơi")
        else:
            update_user_status(user, "Token Lỗi")

    return ok


# ------------------- Chạy trực tiếp -------------------
if __name__ == "__main__":
    username = input("Nhập username: ").strip()

    # Lấy token từ DB qua API
    try:
        resp = requests.get(f"{API_BASE}/api/users/{username}", timeout=5)
        if resp.status_code != 200:
            print(f"❌ Không tìm thấy user {username} (API trả {resp.status_code})")
            exit(1)
        user_data = resp.json()
        jwt = user_data.get("jwt")
        proxy = user_data.get("proxy")
        if not jwt:
            print(f"⚠️ User [{username}] chưa có jwt trong DB")
            exit(1)
        if not (proxy and str(proxy).strip()):
            print(f"⚠️ User [{username}] chưa có proxy trong DB")
            exit(1)
    except Exception as e:
        print(f"❌ Lỗi gọi API lấy user {username}: {e}")
        exit(1)

    print(f"🔍 Đang kiểm tra JWT (WS) cho user [{username}] ...")

    ok = asyncio.run(test_token(jwt, str(proxy).strip(), user=username))

    if ok:
        print(f"✅ Token hợp lệ cho user [{username}]")
    else:
        print(f"❌ Token Lỗi cho user [{username}]")
