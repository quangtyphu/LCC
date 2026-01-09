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
EXTRA_HEADERS = {
    "Origin": "https://play.lc79.bet",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Accept-Language": "vi-VN,vi;q=0.9,fr-FR;q=0.8,fr;q=0.7,en-US;q=0.6,en;q=0.5",
}

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


async def connect_minigame(username: str, keep_alive: bool = False):
    """Kết nối WS minigame một lần, chỉ in raw Socket.IO event (42/minigame, ...)."""

    user = _fetch_user(username)
    if not user:
        return

    proxy_str = user.get("proxy") or ""
    if not proxy_str:
        return

    jwt_token = await _ensure_jwt(username, user)
    if not jwt_token:
        return

    try:
        host, port, puser, ppass = _build_proxy(proxy_str)
    except Exception:
        return

    sock = socks.socksocket()
    sock.set_proxy(socks.SOCKS5, host, port, True, puser, ppass)
    sock.setblocking(False)
    try:
        sock.connect(("wlb.tele68.com", 443))
    except Exception:
        return

    ws = None
    try:
        ws = await websockets.connect(
            WS_URL,
            sock=sock,
            ssl=True,
            ping_interval=None,
            extra_headers=EXTRA_HEADERS,
        )

        ping_interval_ms = 25000
        ping_timeout_ms = 20000
        recv_timeout = 45
        try:
            handshake = await asyncio.wait_for(ws.recv(), timeout=5)
            if isinstance(handshake, str) and handshake.startswith("0"):
                payload = json.loads(handshake[1:])
                ping_interval_ms = int(payload.get("pingInterval", ping_interval_ms))
                ping_timeout_ms = int(payload.get("pingTimeout", ping_timeout_ms))
                recv_timeout = ping_interval_ms / 1000 + ping_timeout_ms / 1000 + 5
        except Exception:
            recv_timeout = 45

        auth_payload = f"40/minigame,{json.dumps({'token': jwt_token})}"
        await ws.send(auth_payload)

        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
            except Exception:
                break

            if msg == "2":
                with contextlib.suppress(Exception):
                    await ws.send("3")
                continue

            if msg.startswith("42/minigame,"):
                try:
                    event_data = json.loads(msg[len("42/minigame,"):])
                    if isinstance(event_data, list) and event_data and event_data[0] == "DEPOSIT_DONE":
                        print(f"📩 [{username}] {msg}")
                except Exception:
                    pass

    except Exception:
        pass
    finally:
        if ws is not None:
            with contextlib.suppress(Exception):
                if ws.open:
                    await ws.send("41/minigame")
                    await asyncio.sleep(0.5)
                    await asyncio.wait_for(ws.close(), timeout=2.0)
        with contextlib.suppress(Exception):
            sock.close()

    return


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
