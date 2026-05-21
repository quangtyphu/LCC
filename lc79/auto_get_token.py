

import requests, json, time, websocket, threading, sys, traceback

CDP_JSON_URL = "http://localhost:9222/json"   # giữ mặc định; nếu bạn dùng cổng khác thì sửa
TARGET_DOMAIN = "play.lc79.bet"
TARGET_PATH_SNIPPET = "/v1/lobby/auth/login"

# Utility to get webSocketDebuggerUrl (chọn tab chứa TARGET_DOMAIN nếu có)
def get_ws_url():
    try:
        r = requests.get(CDP_JSON_URL, timeout=3)
        tabs = r.json()
    except Exception as e:
        print("Không thể truy vấn", CDP_JSON_URL, "- hãy chắc đã forward: adb forward tcp:9222 localabstract:chrome_devtools_remote")
        print("Chi tiết:", e)
        return None
    if not tabs:
        print("Không thấy tab nào trong DevTools. Mở chrome trên device và thử lại.")
        return None
    # tìm tab có target domain
    for t in tabs:
        url = t.get("url") or ""
        if TARGET_DOMAIN in url:
            return t.get("webSocketDebuggerUrl")
    # fallback: return first tab
    return tabs[0].get("webSocketDebuggerUrl")

# Thread-safe send/recv via websocket
class CDPClient:
    def __init__(self, ws_url):
        self.ws_url = ws_url
        self.ws = None
        self._id = 0
        self._lock = threading.Lock()
        self._responses = {}
        self._listeners = []
        self._running = False

    def connect(self):
        print("Connecting to CDP websocket:", self.ws_url)
        self.ws = websocket.create_connection(self.ws_url, timeout=5)
        self._running = True
        # start recv thread
        t = threading.Thread(target=self._recv_loop, daemon=True)
        t.start()

    def _recv_loop(self):
        while self._running:
            try:
                raw = self.ws.recv()
                if not raw:
                    continue
                msg = json.loads(raw)
                # store response by id
                if "id" in msg:
                    rid = msg["id"]
                    with self._lock:
                        self._responses[rid] = msg
                # notify listeners for events (method)
                if "method" in msg:
                    for fn in list(self._listeners):
                        try:
                            fn(msg)
                        except Exception:
                            traceback.print_exc()
            except websocket.WebSocketConnectionClosedException:
                print("WebSocket closed.")
                self._running = False
                break
            except Exception as e:
                print("Recv error:", e)
                time.sleep(0.2)

    def send(self, method, params=None):
        with self._lock:
            self._id += 1
            msg_id = self._id
        payload = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params
        self.ws.send(json.dumps(payload))
        # wait for response (simple)
        for _ in range(50):  # wait up to ~5s
            with self._lock:
                if msg_id in self._responses:
                    resp = self._responses.pop(msg_id)
                    return resp
            time.sleep(0.1)
        return None

    def add_listener(self, fn):
        self._listeners.append(fn)

    def close(self):
        self._running = False
        try:
            self.ws.close()
        except:
            pass

def main_loop():
    ws_url = get_ws_url()
    if not ws_url:
        return
    client = CDPClient(ws_url)
    try:
        client.connect()
    except Exception as e:
        print("Không thể connect tới websocket CDP:", e)
        return

    # enable Network domain
    print("Enabling Network domain...")
    client.send("Network.enable", {"maxTotalBufferSize": 10000000, "maxResourceBufferSize": 5000000})

    # Map to track requestId -> responseReceived params
    # We'll listen for Network.responseReceived events and request body for matching urls.
    def on_event(msg):
        try:
            method = msg.get("method")
            if method == "Network.responseReceived":
                params = msg.get("params", {})
                response = params.get("response", {})
                url = response.get("url", "")
                # filter by snippet
                if TARGET_PATH_SNIPPET in url:
                    requestId = params.get("requestId")
                    print("\n[+] Detected response for:", url)
                    # ask for response body
                    resp = client.send("Network.getResponseBody", {"requestId": requestId})
                    if resp and "result" in resp:
                        body = resp["result"].get("body", "")
                        try:
                            j = json.loads(body)
                        except Exception:
                            # sometimes body is base64-encoded (if base64Encoded true)
                            if resp["result"].get("base64Encoded"):
                                import base64
                                try:
                                    body = base64.b64decode(body).decode("utf-8", errors="ignore")
                                    j = json.loads(body)
                                except Exception:
                                    j = None
                            else:
                                j = None
                        if isinstance(j, dict):
                            # print token if present
                            token = j.get("token") or j.get("accessToken") or (j.get("remoteLoginResp") or {}).get("accessToken")
                            if token:
                                print(">>> TOKEN:", token)
                                print("Full response JSON:")
                                print(json.dumps(j, indent=2, ensure_ascii=False))
                            else:
                                print("Response JSON but no 'token' field. Full JSON printed:")
                                print(json.dumps(j, indent=2, ensure_ascii=False))
                        else:
                            print("Response body (non-JSON):")
                            print(body[:2000])
                    else:
                        print("Không lấy được body response (không có resp). Raw event:")
                        print(json.dumps(msg, indent=2))
        except Exception as e:
            print("Event processing error:", e)
            traceback.print_exc()

    client.add_listener(on_event)

    print("Đang chờ /v1/lobby/auth/login ... (hãy reload trang hoặc chờ site auto-login). Ctrl+C để dừng.")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("Dừng.")
    finally:
        client.close()

if __name__ == "__main__":
    main_loop()
