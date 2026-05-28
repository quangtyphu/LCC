# -*- coding: utf-8 -*-
"""Bat WebSocket game qua CDP (browser auto-attach + fallback tung tab)."""
from __future__ import annotations

import base64
import json
import threading
import time
import urllib.request
from typing import Any, Callable

try:
    import websocket as ws_lib
except ImportError:
    ws_lib = None  # type: ignore

FrameCallback = Callable[[str, str, str], None]

WS_HOOK_JS = r"""
(() => {
  if (window.__benbetHooked) return;
  window.__benbetHooked = true;
  window.__benbetWsLog = window.__benbetWsLog || [];
  const push = (d, url, data) => {
    try {
      const s = typeof data === 'string' ? data : '';
      window.__benbetWsLog.push({ d, url: String(url||''), data: s, t: Date.now() });
      if (window.__benbetWsLog.length > 800) window.__benbetWsLog.shift();
    } catch (e) {}
  };
  const Orig = window.WebSocket;
  window.WebSocket = function(url, protocols) {
    const u = typeof url === 'string' ? url : (url && url.toString()) || '';
    const ws = protocols !== undefined ? new Orig(url, protocols) : new Orig(url);
    ws.addEventListener('message', (ev) => push('recv', u, ev.data));
    const send0 = ws.send.bind(ws);
    ws.send = function(data) { push('send', u, data); return send0(data); };
    return ws;
  };
  window.WebSocket.prototype = Orig.prototype;
  Object.assign(window.WebSocket, { CONNECTING: Orig.CONNECTING, OPEN: Orig.OPEN,
    CLOSING: Orig.CLOSING, CLOSED: Orig.CLOSED });
})();
"""


def _url_match(url: str) -> bool:
    u = (url or "").lower()
    return any(h in u for h in ("taixiu.", "signalr", "luckydice", "3dbenbet.net"))


def _is_game_page(url: str) -> bool:
    return "3dbenbet.net" in (url or "").lower()


def _fetch_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=8) as r:
        return json.loads(r.read().decode())


def _frame_from_network(method: str, params: dict, ws_map: dict[str, str]) -> tuple[str, str, str] | None:
    if method == "Network.webSocketCreated":
        rid = str(params.get("requestId", ""))
        url = str(params.get("url", ""))
        if rid and url:
            ws_map[rid] = url
        if url and _url_match(url):
            return ("", url, "")  # signal only
        return None
    if method not in ("Network.webSocketFrameSent", "Network.webSocketFrameReceived"):
        return None
    rid = str(params.get("requestId", ""))
    url = ws_map.get(rid, "")
    resp = params.get("response") or {}
    data = str(resp.get("payloadData") or "")
    if resp.get("opcode") == 2 and data:
        try:
            data = base64.b64decode(data).decode("utf-8", errors="replace")
        except Exception:
            pass
    if not data or not _url_match(url):
        return None
    direction = "ws_send" if "Sent" in method else "ws_recv"
    return (direction, url, data)


class _BrowserCdpSniffer:
    """Ket noi CDP cap browser — bat WS moi tab/iframe (flatten)."""

    def __init__(self, cdp_base: str, on_frame: FrameCallback) -> None:
        self.cdp_base = cdp_base.rstrip("/")
        self.on_frame = on_frame
        self._ws: Any = None
        self._id = 0
        self._lock = threading.Lock()
        self._running = False
        self._ready = threading.Event()
        self._ws_urls: dict[str, str] = {}
        self._enabled_sessions: set[str] = set()
        self._frame_count = 0

    @property
    def frame_count(self) -> int:
        return self._frame_count

    def _send(self, method: str, params: dict | None = None, *, session_id: str | None = None) -> None:
        if not self._ws:
            return
        with self._lock:
            self._id += 1
            mid = self._id
        msg: dict[str, Any] = {"id": mid, "method": method}
        if params:
            msg["params"] = params
        if session_id:
            msg["sessionId"] = session_id
        try:
            self._ws.send(json.dumps(msg))
        except Exception:
            pass

    def _enable_session(self, session_id: str) -> None:
        if not session_id or session_id in self._enabled_sessions:
            return
        self._enabled_sessions.add(session_id)
        self._send("Network.enable", {"maxTotalBufferSize": 80_000_000}, session_id=session_id)
        self._send("Runtime.enable", {}, session_id=session_id)
        self._send(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": WS_HOOK_JS},
            session_id=session_id,
        )
        self._send(
            "Runtime.evaluate",
            {"expression": WS_HOOK_JS, "returnByValue": True},
            session_id=session_id,
        )
        print(f"[CDP] Gan session ({session_id[:8]}…)", flush=True)
        self._ready.set()

    def _on_message(self, _ws: Any, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        method = msg.get("method") or ""
        params = msg.get("params") or {}
        session_id = msg.get("sessionId") or ""

        if method == "Target.attachedToTarget":
            sid = params.get("sessionId", "")
            self._enable_session(str(sid))
            return

        if method.startswith("Network.webSocket"):
            out = _frame_from_network(method, params, self._ws_urls)
            if out:
                direction, url, data = out
                if direction:
                    self._frame_count += 1
                    self._ready.set()
                    self.on_frame(direction, url, data)
                elif url:
                    print(f"[CDP] WS: {url[:100]}", flush=True)
                    self._ready.set()

        # JS hook log (poll via separate evaluate — optional, skip for browser path)

    def start(self) -> bool:
        if ws_lib is None:
            return False
        try:
            ver = _fetch_json(f"{self.cdp_base}/json/version")
            browser_ws = ver.get("webSocketDebuggerUrl")
        except Exception as exc:
            print(f"[CDP] Khong lay browser WS: {exc}", flush=True)
            return False
        if not browser_ws:
            return False
        self._ws = ws_lib.WebSocketApp(browser_ws, on_message=self._on_message)
        self._running = True
        threading.Thread(
            target=lambda: self._ws.run_forever(ping_interval=20, ping_timeout=10),
            daemon=True,
        ).start()
        time.sleep(0.5)
        self._send("Target.setDiscoverTargets", {"discover": True})
        self._send(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
        )
        print("[CDP] Browser sniffer bat dau (auto-attach moi tab)", flush=True)
        return True

    def stop(self) -> None:
        self._running = False
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass


class _TabCdpSniffer:
    """Fallback: gan truc tiep tab play.3dbenbet.net."""

    def __init__(self, cdp_base: str, on_frame: FrameCallback) -> None:
        self.cdp_base = cdp_base.rstrip("/")
        self.on_frame = on_frame
        self._sessions: dict[str, Any] = {}
        self._skip: set[str] = set()
        self._ready = threading.Event()
        self._frame_count = 0

    @property
    def frame_count(self) -> int:
        return self._frame_count

    def _try_attach(self, target: dict) -> bool:
        tid = str(target.get("id", ""))
        if not tid or tid in self._skip or tid in self._sessions:
            return False
        url = str(target.get("url", ""))
        wss = target.get("webSocketDebuggerUrl")
        if not wss or target.get("type") not in ("page", "iframe"):
            return False
        if not _is_game_page(url):
            return False
        if "devtools://" in url:
            return False
        try:
            sess = _SingleTab(wss, self._on_frame_wrapped)
            sess.start()
            self._sessions[tid] = sess
            print(f"[CDP] Tab game: {url[:85]}", flush=True)
            self._ready.set()
            return True
        except Exception:
            self._skip.add(tid)
            return False

    def _on_frame_wrapped(self, direction: str, url: str, data: str) -> None:
        self._frame_count += 1
        self._ready.set()
        self.on_frame(direction, url, data)

    def poll(self) -> None:
        try:
            targets = _fetch_json(f"{self.cdp_base}/json/list")
        except Exception:
            return
        live = {str(t.get("id", "")) for t in targets}
        for tid in list(self._sessions.keys()):
            if tid not in live:
                self._sessions.pop(tid, None)
        for t in targets:
            self._try_attach(t)
        for sess in list(self._sessions.values()):
            sess.poll_js(self._on_frame_wrapped)

    def stop(self) -> None:
        for s in self._sessions.values():
            s.stop()
        self._sessions.clear()


class _SingleTab:
    def __init__(self, wss: str, on_frame: FrameCallback) -> None:
        self._wss = wss
        self.on_frame = on_frame
        self._ws: Any = None
        self._ws_urls: dict[str, str] = {}
        self._alive = False
        self._id = 0
        self._responses: dict[int, dict] = {}

    def _send(self, method: str, params: dict | None = None) -> dict | None:
        if not self._alive or not self._ws:
            return None
        self._id += 1
        mid = self._id
        try:
            self._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        except Exception:
            self._alive = False
            return None
        for _ in range(40):
            if mid in self._responses:
                return self._responses.pop(mid)
            if not self._alive:
                return None
            time.sleep(0.04)
        return None

    def _on_message(self, _w: Any, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if "id" in msg:
            self._responses[msg["id"]] = msg
            return
        method = msg.get("method") or ""
        if not method.startswith("Network."):
            return
        out = _frame_from_network(method, msg.get("params") or {}, self._ws_urls)
        if out and out[0]:
            self.on_frame(out[0], out[1], out[2])
        elif out and out[1]:
            print(f"[CDP] WS: {out[1][:100]}", flush=True)

    def start(self) -> None:
        if ws_lib is None:
            raise RuntimeError("websocket-client")
        self._ws = ws_lib.WebSocketApp(self._wss, on_message=self._on_message)
        threading.Thread(
            target=lambda: self._ws.run_forever(ping_interval=15, ping_timeout=10),
            daemon=True,
        ).start()
        time.sleep(0.6)
        self._alive = True
        if not self._send("Network.enable", {"maxTotalBufferSize": 80_000_000}):
            self._alive = False
            raise RuntimeError("tab khong phan hoi")
        self._send("Runtime.enable", {})
        self._send("Page.addScriptToEvaluateOnNewDocument", {"source": WS_HOOK_JS})
        self._send("Runtime.evaluate", {"expression": WS_HOOK_JS, "returnByValue": True})

    def poll_js(self, on_frame: FrameCallback) -> None:
        if not self._alive:
            return
        resp = self._send(
            "Runtime.evaluate",
            {
                "expression": (
                    "(function(){var a=window.__benbetWsLog||[];"
                    "window.__benbetWsLog=[];return JSON.stringify(a);})()"
                ),
                "returnByValue": True,
            },
        )
        if not resp:
            return
        val = (resp.get("result") or {}).get("result", {}).get("value")
        if not val:
            return
        try:
            rows = json.loads(val)
        except json.JSONDecodeError:
            return
        for row in rows:
            url = str(row.get("url", ""))
            if not _url_match(url):
                continue
            d = row.get("d", "")
            on_frame("ws_send" if d == "send" else "ws_recv", url, str(row.get("data", "")))

    def stop(self) -> None:
        self._alive = False
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass


class CdpWsSniffer:
    def __init__(self, cdp_base: str, on_frame: FrameCallback) -> None:
        self.cdp_base = cdp_base.rstrip("/")
        self.on_frame = on_frame
        self._browser = _BrowserCdpSniffer(cdp_base, on_frame)
        self._tabs = _TabCdpSniffer(cdp_base, on_frame)
        self._stop = threading.Event()
        self._ready = threading.Event()

    @property
    def frame_count(self) -> int:
        return self._browser.frame_count + self._tabs.frame_count

    def start(self) -> None:
        self._browser.start()
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def wait_ready(self, timeout: float = 90.0) -> bool:
        deadline = time.time() + timeout
        last_msg = 0.0
        while time.time() < deadline and not self._stop.is_set():
            if self.frame_count > 0 or self._browser._ready.is_set() or self._tabs._ready.is_set():
                return True
            self._tabs.poll()
            now = time.time()
            if now - last_msg >= 10:
                print("  ... cho CDP gan tab game (F5 trang neu lau) ...", flush=True)
                last_msg = now
            time.sleep(2)
        return self.frame_count > 0

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            self._tabs.poll()
            time.sleep(2)

    def stop(self) -> None:
        self._stop.set()
        self._browser.stop()
        self._tabs.stop()


def list_cdp_targets(cdp_base: str = "http://127.0.0.1:9223") -> None:
    """In danh sach tab Chrome (debug)."""
    try:
        targets = _fetch_json(f"{cdp_base.rstrip('/')}/json/list")
    except Exception as exc:
        print(f"Loi: {exc}")
        return
    print(f"Tabs ({len(targets)}):")
    for t in targets:
        print(
            f"  [{t.get('type', '?'):8}] {str(t.get('id', ''))[:12]}  "
            f"{str(t.get('url', ''))[:75]}"
        )


if __name__ == "__main__":
    list_cdp_targets()
