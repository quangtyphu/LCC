import asyncio
import json
import websockets
import socks
import socket

WS_URL = "wss://wtx.tele68.com/tx/?EIO=4&transport=websocket"

async def test_token(username, jwt, proxy_str=None):
    ws = None
    try:
        # ⚙️ Tạo kết nối WS (qua proxy nếu có)
        if proxy_str:
            host, port, puser, ppass = proxy_str.split(":")
            port = int(port)
            sock = socks.socksocket()
            sock.set_proxy(socks.SOCKS5, host, port, True, puser, ppass)
            sock.settimeout(10)
            sock.connect(("wtx.tele68.com", 443))
            print(f"🌐 [{username}] Kết nối WS qua proxy {host}:{port}")
            ws = await websockets.connect(WS_URL, sock=sock, ssl=True, ping_interval=None)
        else:
            print(f"🌐 [{username}] Kết nối WS trực tiếp (không proxy)")
            ws = await websockets.connect(WS_URL, ssl=True, ping_interval=None)

        # 📥 Nhận gói handshake đầu tiên
        hello = await ws.recv()
        print(f"📥 Handshake: {hello}")

        # 📤 Gửi join namespace kèm token (giống web thật)
        payload = json.dumps({"token": jwt})
        await ws.send(f"40/tx,{payload}")
        print(f"📤 [{username}] Đã gửi token → chờ phản hồi...")

        # 📡 Lắng nghe phản hồi
        for i in range(10):
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
                print(f"📡 Gói tin [{i+1}]: {msg}")

                # ❌ Nếu server báo lỗi
                if msg.startswith("44/tx,"):
                    raw = msg[len("44/tx,"):]
                    print(f"❌ [{username}] Server báo lỗi: {raw}")
                    return False

                # ✅ Nếu là gói event (42/tx)
                if msg.startswith("42/tx,"):
                    raw = msg[len("42/tx,"):]
                    try:
                        arr = json.loads(raw)
                        print(f"📦 JSON parse: {arr}")
                        event = arr[0]
                        data = arr[1] if len(arr) > 1 else {}
                        # Gói xác nhận login thường là "your-info"
                        if event == "your-info":
                            print(f"✅ [{username}] Token hợp lệ → user: {data.get('username')} nickname: {data.get('nickname')}")
                            return True
                    except Exception as e:
                        print(f"⚠️ [{username}] Lỗi parse JSON: {e}")

            except asyncio.TimeoutError:
                print("⏳ Hết thời gian chờ gói tin kế tiếp")
                break

        print(f"❌ [{username}] Không thấy gói your-info → token có thể sai/hết hạn")
        return False

    except Exception as e:
        print(f"❌ [{username}] Lỗi: {repr(e)}")
        return False
    finally:
        if ws:
            await ws.close()


if __name__ == "__main__":
    username = input("👤 Nhập Username: ").strip()
    jwt = input("🔑 Nhập Token JWT: ").strip()
    proxy = input("🌐 Nhập Proxy (host:port:user:pass) hoặc bỏ trống nếu không dùng: ").strip() or None
    asyncio.run(test_token(username, jwt, proxy))
