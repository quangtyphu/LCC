import os
import sys
import asyncio
import json
import time
import contextlib
import socks
import websockets
import requests

from jwt_manager import refresh_jwt

API_BASE = "http://127.0.0.1:3000"
WS_URL = "wss://wlb.tele68.com/minigame/?EIO=4&transport=websocket"

# Windows needs selector policy for socks sockets
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _build_proxy(proxy_str: str):
    host, port, userp, passp = proxy_str.split(":")
    return host, int(port), userp, passp


def _fetch_user(username: str):
    try:
        r = requests.get(f"{API_BASE}/api/users/{username}", timeout=5)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        print(f"⚠️ [{username}] Lỗi lấy user từ DB: {e}")
        return None


def _update_jwt(username: str, jwt_token: str):
    try:
        requests.put(f"{API_BASE}/api/users/{username}", json={"jwt": jwt_token}, timeout=5)
    except Exception:
        pass


async def _ensure_jwt(username: str, user_data: dict) -> str | None:
    jwt_token = user_data.get("jwt") or ""
    if jwt_token:
        return jwt_token
    print(f"⚠️ [{username}] Không có JWT, thử refresh...")
    jwt_token = await asyncio.to_thread(lambda: refresh_jwt(username))
    if jwt_token:
        _update_jwt(username, jwt_token)
    return jwt_token


async def connect_minigame(username: str):
    user = _fetch_user(username)
    if not user:
        print(f"❌ [{username}] Không tìm thấy user trong DB")
        return

    proxy_str = user.get("proxy") or ""
    if not proxy_str:
        print(f"❌ [{username}] Không có proxy")
        return

    jwt_token = await _ensure_jwt(username, user)
    if not jwt_token:
        print(f"❌ [{username}] Không lấy được JWT")
        return

    try:
        host, port, puser, ppass = _build_proxy(proxy_str)
    except Exception:
        print(f"❌ [{username}] Proxy sai định dạng")
        return

    # Open SOCKS5 socket
    sock = socks.socksocket()
    sock.set_proxy(socks.SOCKS5, host, port, True, puser, ppass)
    sock.setblocking(False)
    try:
        sock.connect(("wlb.tele68.com", 443))
    except Exception:
        print(f"❌ [{username}] Proxy không kết nối được wlb.tele68.com:443")
        with contextlib.suppress(Exception):
            sock.close()
        return

    print(f"🔌 [{username}] Đang kết nối WS minigame...")

    try:
        async with websockets.connect(WS_URL, sock=sock, ssl=True, ping_interval=None) as ws:
            # Bỏ gói chào nếu có
            with contextlib.suppress(Exception):
                await ws.recv()

            auth_payload = f"40/minigame,{json.dumps({'token': jwt_token})}"
            await ws.send(auth_payload)
            print(f"✅ [{username}] Đã gửi token")

            last_msg = time.time()
            while True:
                now = time.time()
                if now - last_msg > 120:
                    print(f"⏳ [{username}] Timeout 120s, thoát")
                    break

                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    last_msg = now
                except asyncio.TimeoutError:
                    try:
                        await ws.send("3")
                        print(f"💓 [{username}] Gửi ping giữ kết nối")
                        continue
                    except Exception as e:
                        print(f"⚠️ [{username}] Lỗi gửi ping: {e}")
                        break
                except Exception as e:
                    print(f"💥 [{username}] Lỗi WS: {e}")
                    break

                if msg == "2":
                    try:
                        await ws.send("3")
                        continue
                    except Exception:
                        break

                print(f"📩 [{username}] {msg}")
    except Exception as e:
        print(f"❌ [{username}] Không mở được WS: {e}")
    finally:
        with contextlib.suppress(Exception):
            sock.close()


def main():
    if len(sys.argv) >= 2:
        username = sys.argv[1].strip()
    else:
        username = input("Nhập username: ").strip()
        if not username:
            print("❌ Chưa nhập username")
            return

    try:
        asyncio.run(connect_minigame(username))
    except KeyboardInterrupt:
        print("\n⏹️ Đã dừng theo yêu cầu")


if __name__ == "__main__":
    main()
