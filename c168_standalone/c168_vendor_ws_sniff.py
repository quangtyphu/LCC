# -*- coding: utf-8 -*-
"""
Bắt WebSocket vendor (h54uk / Bikimex) qua Chrome CDP — token luôn mới từ tab đang chơi.

Cách dùng:
  1. python c168_login_open_game.py -u ... -p ...  (Chrome port 9340)
  2. Vào 1 bàn trong game vendor
  3. python c168_listen_ws.py -u USER --table-id 1006

Không copy curl wss://... — JWT sống ~30s–2 phút và khóa IP.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import websocket as ws_lib
except ImportError:
    print("pip install websocket-client", file=sys.stderr)
    raise

from c168_capture_game_b import CDP_URL
from c168_vendor_keepalive import VENDOR_ANTI_IDLE_JS

CDP_DEFAULT = CDP_URL  # 9340 — cung Chrome voi c168_login_open_game.py

ROUND_HINTS = re.compile(
    r"round|shoe|game|result|bet|deal|card|banker|player|tie|countdown|"
    r"phase|roundId|gameRound|betting|settle|balance|gold|credit",
    re.I,
)

FrameCallback = Callable[[str, str, str], None]

WS_HOOK_JS = r"""
(() => {
  if (window.__c168WsHooked) return;
  window.__c168WsHooked = true;
  window.__c168WsLog = window.__c168WsLog || [];
  const push = (d, url, data) => {
    try {
      let s = '';
      if (typeof data === 'string') s = data;
      else if (data instanceof ArrayBuffer) {
        const b = new Uint8Array(data);
        if (b.length >= 3 && b[0] === 0xD9) {
          const ln = b[1];
          s = new TextDecoder().decode(b.subarray(2, Math.min(2 + ln, b.length)));
        } else {
          let i = 0;
          for (; i < b.length; i++) if (b[i] === 0x7b) break;
          s = i < b.length ? new TextDecoder().decode(b.subarray(i)) : '';
        }
      }
      window.__c168WsLog.push({ d, url: String(url||''), data: s, t: Date.now() });
      if (window.__c168WsLog.length > 1200) window.__c168WsLog.shift();
    } catch (e) {}
  };
  window.__c168JkSockets = window.__c168JkSockets || [];
  const Orig = window.WebSocket;
  window.WebSocket = function(url, protocols) {
    const u = typeof url === 'string' ? url : (url && url.toString()) || '';
    const ws = protocols !== undefined ? new Orig(url, protocols) : new Orig(url);
    if (/jk17y|delta9968\.com\/jk/i.test(u)) {
      window.__c168JkSockets.push({ url: u, ws });
    }
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

JS_DRAIN_WS_LOG = r"""
() => {
  const buf = window.__c168WsLog || [];
  const out = buf.splice(0, buf.length);
  return out;
}
"""

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _url_match(url: str) -> bool:
    u = (url or "").lower()
    if "analysiscloud" in u:
        return False
    return any(
        k in u
        for k in (
            "h54uk",
            "bpcdf.",
            "tgmeq",
            "mhuxu",
            "bikimex",
            "delta9968.com/jk",
            "/jk17y",
        )
    )


def _is_vendor_page(url: str) -> bool:
    u = (url or "").lower()
    return any(k in u for k in ("bpcdf.", "tgmeq", "mhuxu", "bikimex"))


def decode_frame(data: str) -> str:
    raw = data
    if not raw:
        return ""
    # CDP có thể gửi binary dạng base64 trong một số case — thử decode
    if len(raw) > 2 and not raw.lstrip().startswith(("{", "[")):
        try:
            b = base64.b64decode(raw, validate=False)
            if len(b) >= 3 and b[0] == 0xD9:
                ln = b[1]
                body = b[2 : 2 + ln]
                if len(body) == ln:
                    return body.decode("utf-8", errors="replace")
        except Exception:
            pass
        # hex-ish
        try:
            if all(c in "0123456789abcdef" for c in raw[:40].lower()) and len(raw) % 2 == 0:
                b = bytes.fromhex(raw[:200])
                if len(b) >= 3 and b[0] == 0xD9:
                    ln = b[1]
                    return b[2 : 2 + ln].decode("utf-8", errors="replace")
        except Exception:
            pass
        try:
            b = raw.encode("latin-1", errors="surrogateescape")
            if len(b) >= 3 and b[0] == 0xD9:
                ln = b[1]
                body = b[2 : 2 + ln]
                if len(body) == ln:
                    return body.decode("utf-8", errors="replace")
        except Exception:
            pass
    idx = raw.find("{")
    if idx > 0:
        return raw[idx:]
    return raw


def _fetch_json(url: str) -> Any:
    import urllib.request

    with urllib.request.urlopen(url, timeout=8) as r:
        return json.loads(r.read().decode())


def _frame_from_network(method: str, params: dict, ws_map: dict[str, str]) -> tuple[str, str, str] | None:
    if method == "Network.webSocketCreated":
        rid = str(params.get("requestId", ""))
        url = str(params.get("url", ""))
        if rid and url:
            ws_map[rid] = url
        if url and _url_match(url):
            return ("", url, "")
        return None
    if method not in ("Network.webSocketFrameSent", "Network.webSocketFrameReceived"):
        return None
    rid = str(params.get("requestId", ""))
    url = ws_map.get(rid, "")
    resp = params.get("response") or {}
    data = str(resp.get("payloadData") or "")
    opcode = resp.get("opcode")
    if data and not data.lstrip().startswith(("{", "[")):
        raw_b: bytes | None = None
        try:
            if opcode == 2:
                raw_b = base64.b64decode(data, validate=False)
            else:
                raw_b = data.encode("latin-1", errors="surrogateescape")
        except Exception:
            raw_b = None
        if raw_b:
            try:
                if len(raw_b) >= 3 and raw_b[0] == 0xD9:
                    ln = raw_b[1]
                    body = raw_b[2 : 2 + ln]
                    if len(body) == ln:
                        data = body.decode("utf-8", errors="replace")
                else:
                    idx = raw_b.find(b"{")
                    if idx >= 0:
                        data = raw_b[idx:].decode("utf-8", errors="replace")
            except Exception:
                pass
    if not _url_match(url):
        return None
    direction = "send" if "Sent" in method else "recv"
    return (direction, url, data)


class BrowserCdpSniffer:
    def __init__(self, cdp_base: str, on_frame: FrameCallback) -> None:
        self.cdp_base = cdp_base.rstrip("/")
        self.on_frame = on_frame
        self._ws: Any = None
        self._id = 0
        self._lock = threading.Lock()
        self._ws_urls: dict[str, str] = {}
        self._enabled: set[str] = set()
        self._session_urls: dict[str, str] = {}
        self._poll_stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self.frame_count = 0
        self.h54uk_frame_count = 0
        self.h54uk_urls: list[str] = []
        self.jk17y_urls: list[str] = []
        self._attached_targets: set[str] = set()

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

    def _enable_session(self, session_id: str, *, url: str = "") -> None:
        if not session_id or session_id in self._enabled:
            return
        self._enabled.add(session_id)
        if url:
            self._session_urls[session_id] = url
        self._send("Network.enable", {"maxTotalBufferSize": 80_000_000}, session_id=session_id)
        self._send("Runtime.enable", {}, session_id=session_id)
        hook = WS_HOOK_JS + "\n" + VENDOR_ANTI_IDLE_JS
        self._send("Page.addScriptToEvaluateOnNewDocument", {"source": hook}, session_id=session_id)
        self._send("Runtime.evaluate", {"expression": hook, "returnByValue": True}, session_id=session_id)
        try:
            self._send(
                "Emulation.setFocusEmulationEnabled",
                {"enabled": True},
                session_id=session_id,
            )
        except Exception:
            pass
        if len(self._enabled) <= 12:
            print(f"[{_ts()}] CDP gắn session {session_id[:10]}…", flush=True)

    def _emit_frame(self, direction: str, url: str, data: str) -> None:
        if not direction or not _url_match(url):
            return
        if "h54uk" in url.lower() and url not in self.h54uk_urls:
            self.h54uk_urls.append(url)
            print(f"[{_ts()}] ★ h54uk mới: {url[:120]}…", flush=True)
        if ("jk17y" in url.lower() or "delta9968" in url.lower()) and url not in self.jk17y_urls:
            self.jk17y_urls.append(url)
        self.frame_count += 1
        if "h54uk" in url.lower():
            self.h54uk_frame_count += 1
        try:
            self.on_frame(direction, url, data)
        except Exception as exc:
            if not getattr(self, "_on_frame_err_logged", False):
                self._on_frame_err_logged = True
                print(f"[{_ts()}] on_frame lỗi: {exc}", flush=True)

    def _on_message(self, _ws: Any, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if "id" in msg and "result" in msg:
            self._on_cdp_result(msg.get("result"))
            return
        method = msg.get("method") or ""
        params = msg.get("params") or {}
        if method == "Target.attachedToTarget":
            sid = str(params.get("sessionId", ""))
            info = params.get("targetInfo") if isinstance(params.get("targetInfo"), dict) else {}
            tid = str(info.get("targetId") or "")
            if tid:
                self._attached_targets.add(tid)
            self._enable_session(sid, url=str(info.get("url") or ""))
            return
        out = _frame_from_network(method, params, self._ws_urls)
        if not out:
            return
        direction, url, data = out
        if url and not direction:
            ul = url.lower()
            if "h54uk" in ul:
                if url not in self.h54uk_urls:
                    self.h54uk_urls.append(url)
                    print(f"[{_ts()}] ★ h54uk kết nối: {url[:120]}…", flush=True)
            else:
                print(f"[{_ts()}] WS: {url[:100]}", flush=True)
            return
        if direction:
            self._emit_frame(direction, url, data)

    def _attach_existing_targets(self) -> None:
        try:
            targets = _fetch_json(f"{self.cdp_base}/json/list")
        except Exception:
            return
        if not isinstance(targets, list):
            return
        for t in targets:
            if not isinstance(t, dict):
                continue
            tid = str(t.get("id") or "")
            if not tid or tid in self._attached_targets:
                continue
            self._attached_targets.add(tid)
            self._send(
                "Target.attachToTarget",
                {"targetId": tid, "flatten": True},
            )
            time.sleep(0.02)

    def _poll_js_logs(self) -> None:
        while not self._poll_stop.is_set():
            for sid in list(self._enabled):
                try:
                    self._send(
                        "Runtime.evaluate",
                        {
                            "expression": JS_DRAIN_WS_LOG,
                            "returnByValue": True,
                            "awaitPromise": False,
                        },
                        session_id=sid,
                    )
                except Exception:
                    pass
            time.sleep(0.7)

    def _on_cdp_result(self, result: Any) -> None:
        if not isinstance(result, dict):
            return
        inner = result.get("result")
        if not isinstance(inner, dict):
            return
        val = inner.get("value")
        if not isinstance(val, list):
            return
        for item in val:
            if not isinstance(item, dict):
                continue
            direction = str(item.get("d") or "")
            url = str(item.get("url") or "")
            data = str(item.get("data") or "")
            if direction and url:
                self._emit_frame(direction, url, data)

    def reload_vendor_tables(self) -> int:
        """Không F5 trang vendor — reload đẩy về gamehallBackToGame (mất bàn C06)."""
        return 0

    def reinject_ws_hooks(self) -> None:
        """Gắn lại hook WebSocket trên mọi session (sau khi vào bàn / iframe mới)."""
        hook = WS_HOOK_JS + "\n" + VENDOR_ANTI_IDLE_JS
        for sid in list(self._enabled):
            try:
                self._send(
                    "Runtime.evaluate",
                    {"expression": hook, "returnByValue": True},
                    session_id=sid,
                )
            except Exception:
                pass

    def reset_h54uk_counters(self) -> None:
        self.h54uk_frame_count = 0
        self.frame_count = 0

    def wait_h54uk(self, timeout_sec: float = 20) -> bool:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if self.h54uk_frame_count > 0 or self.h54uk_urls:
                return True
            time.sleep(0.5)
        return False

    def start(self) -> bool:
        try:
            ver = _fetch_json(f"{self.cdp_base}/json/version")
            browser_ws = ver.get("webSocketDebuggerUrl")
        except Exception as exc:
            print(f"CDP không chạy tại {self.cdp_base}: {exc}", flush=True)
            print("  → Chạy c168_login_open_game.py (Chrome port 9340)", flush=True)
            return False
        if not browser_ws:
            return False
        self._ws = ws_lib.WebSocketApp(browser_ws, on_message=self._on_message)
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
        time.sleep(0.6)
        self._attach_existing_targets()
        time.sleep(0.8)
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(target=self._poll_js_logs, daemon=True)
        self._poll_thread.start()
        print(
            f"[{_ts()}] CDP sniffer OK — đã gắn {len(self._enabled)} session",
            flush=True,
        )
        return True

    def stop(self) -> None:
        self._poll_stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass


_ACTIVE_SNIFFER: BrowserCdpSniffer | None = None


def prime_ws_capture(cdp_base: str) -> BrowserCdpSniffer:
    """Bật Network/hook trước khi vào bàn — tránh lỡ WS h54uk đã mở."""
    global _ACTIVE_SNIFFER
    if _ACTIVE_SNIFFER is not None:
        try:
            _ACTIVE_SNIFFER.reinject_ws_hooks()
            _ACTIVE_SNIFFER._attach_existing_targets()
        except Exception:
            pass
        return _ACTIVE_SNIFFER

    def _noop(*_args: Any) -> None:
        return

    sniffer = BrowserCdpSniffer(cdp_base, _noop)
    if not sniffer.start():
        raise RuntimeError(f"CDP sniffer không start: {cdp_base}")
    _ACTIVE_SNIFFER = sniffer
    return sniffer


def take_active_sniffer() -> BrowserCdpSniffer | None:
    global _ACTIVE_SNIFFER
    s = _ACTIVE_SNIFFER
    _ACTIVE_SNIFFER = None
    return s


def refresh_sniffer_targets() -> None:
    """Gắn lại target sau khi mở tab/iframe vendor mới."""
    if _ACTIVE_SNIFFER is not None:
        _ACTIVE_SNIFFER._attach_existing_targets()
        time.sleep(0.6)


def adopt_or_create_sniffer(
    cdp_base: str,
    on_frame: FrameCallback,
    *,
    existing: BrowserCdpSniffer | None = None,
) -> BrowserCdpSniffer:
    sniffer = existing or take_active_sniffer()
    if sniffer is not None:
        sniffer.on_frame = on_frame
        return sniffer
    sniffer = BrowserCdpSniffer(cdp_base, on_frame)
    if not sniffer.start():
        raise RuntimeError("CDP sniffer start failed")
    return sniffer


def _gameinfo_table_id(obj: dict[str, Any]) -> int | None:
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    for block in (obj.get("tableInfo"), msg.get("tableInfo"), msg):
        if isinstance(block, dict) and block.get("tableID") is not None:
            try:
                return int(block["tableID"])
            except (TypeError, ValueError):
                pass
    return None


def is_table_enter_message(obj: dict[str, Any], table_id: int) -> bool:
    """Đã focus bàn: GameInfo (không phải GameHallInfo) đúng tableID.

    - handler 2 + tableInfo: đầu ván mới (capture cũ).
    - handler 1 + tableName: vào giữa ván (live probe 2026-05-24).
    """
    if not isinstance(obj, dict) or str(obj.get("messageType") or "") != "GameInfo":
        return False
    try:
        if _gameinfo_table_id(obj) != int(table_id):
            return False
    except (TypeError, ValueError):
        return False
    handler = obj.get("handler")
    if handler == 2:
        ti = obj.get("tableInfo")
        if not isinstance(ti, dict):
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            ti = msg.get("tableInfo")
        return isinstance(ti, dict)
    if handler == 1:
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        return bool(msg.get("tableName"))
    return False


def run_sniff(
    duration_s: int,
    cdp_base: str,
    log_path: Path | None,
) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    log_f = log_path.open("a", encoding="utf-8") if log_path else None

    def on_frame(direction: str, url: str, data: str) -> None:
        text = decode_frame(data)
        short_url = url.split("?")[0]
        if "h54uk" in url:
            tag = "H54UK"
        elif "jk17y" in url or "delta9968" in url:
            tag = "TEL"
        else:
            tag = "WS"
        line = f"[{_ts()}] [{tag}] {direction} {text[:700]}"
        print(line, flush=True)
        if ROUND_HINTS.search(text):
            print(f"         >>> round/bet hint", flush=True)
        if log_f:
            rec = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "dir": direction,
                "url": url,
                "body": text[:8000],
            }
            log_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            log_f.flush()

    sniffer = BrowserCdpSniffer(cdp_base, on_frame)
    if not sniffer.start():
        return 1

    print(f"Lắng nghe {duration_s}s — vào bàn + đợi vài ván…\n", flush=True)
    t0 = time.time()
    last_n = 0
    while time.time() - t0 < duration_s:
        time.sleep(5)
        if sniffer.frame_count > last_n:
            last_n = sniffer.frame_count
        elif time.time() - t0 > 15:
            print(f"  … chưa có frame (đã {int(time.time()-t0)}s) — F5 trang game vendor?", flush=True)

    sniffer.stop()
    if log_f:
        log_f.close()
    print(f"\n=== Xong: {sniffer.frame_count} frame, {len(sniffer.h54uk_urls)} URL h54uk ===", flush=True)
    for u in sniffer.h54uk_urls[-3:]:
        print(f"  {u[:140]}", flush=True)
    return 0 if sniffer.frame_count else 2


def main() -> int:
    ap = argparse.ArgumentParser(description="Bắt WS h54uk qua Chrome CDP (token tự mới)")
    ap.add_argument("duration", nargs="?", type=int, default=120, help="giây")
    ap.add_argument("--cdp", default=CDP_DEFAULT, help="http://127.0.0.1:9333")
    ap.add_argument(
        "--log",
        default="",
        help="file .jsonl (mặc định: capture_logs/vendor_ws_<ts>.jsonl)",
    )
    ap.add_argument("--list-tabs", action="store_true")
    args = ap.parse_args()

    if args.list_tabs:
        try:
            for t in _fetch_json(f"{args.cdp.rstrip('/')}/json/list"):
                print(f"  [{t.get('type')}] {t.get('url', '')[:90]}")
        except Exception as e:
            print(f"Lỗi: {e}")
        return 1

    log_path: Path | None = None
    if args.log:
        log_path = Path(args.log)
    else:
        out = Path(__file__).resolve().parent / "capture_logs"
        out.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = out / f"vendor_ws_{stamp}.jsonl"

    return run_sniff(args.duration, args.cdp.rstrip("/"), log_path)


if __name__ == "__main__":
    raise SystemExit(main())
