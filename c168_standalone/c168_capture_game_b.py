# -*- coding: utf-8 -*-
"""
Chrome mới (không proxy) — bạn đăng nhập tay → click Game B → tắt Chrome.
Ghi toàn bộ /hall/api/ + URL vendor; khi đóng Chrome → phân tích cần gì để mở Game B không browser.

  python c168_capture_game_b.py
  python c168_capture_game_b.py --url https://c1686.net
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

try:
    import websocket as ws_lib
except ImportError:
    ws_lib = None  # type: ignore

from c168_api_analyze import analyze_api_events, print_analysis_summary
from c168_config_util import load_config
from c168_register import _find_chrome_exe, _log_append

_DIR = Path(__file__).resolve().parent
CAPTURE_PROFILE = "c168-gameb-capture"
CAPTURE_PORT = 9340
CDP_URL = f"http://127.0.0.1:{CAPTURE_PORT}"

_chrome: dict[str, Any] = {
    "cdp_url": CDP_URL,
    "profile_dir": "",
    "cms": False,
}


def configure_chrome_session(
    cdp_url: str,
    profile_dir: str = "",
    *,
    cms: bool = False,
) -> None:
    """Gắn CDP/profile CMS (Quản lý Chrome) — gọi trước login/auto bet."""
    _chrome["cdp_url"] = (cdp_url or CDP_URL).rstrip("/")
    _chrome["profile_dir"] = profile_dir or ""
    _chrome["cms"] = bool(cms)


def current_cdp_url() -> str:
    return str(_chrome.get("cdp_url") or CDP_URL)


def is_cms_chrome_session() -> bool:
    return bool(_chrome.get("cms"))

HALL_RE = re.compile(r"/hall/api/", re.I)
VENDOR_RE = re.compile(r"bpcdf\.|tgmeq|mhuxu|bikimex", re.I)
GAME_LOGIN_RE = re.compile(r"gameCenter/gameApi/login", re.I)

SESSION_HEADER_KEYS = (
    "token",
    "newjwt",
    "device",
    "browserfingerid",
    "sitecode",
    "domain",
    "currency",
    "x-device",
    "x-data-mode",
    "x-version",
    "appversion",
)


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _profile_dir() -> str:
    return os.path.join(os.environ.get("TEMP", "."), CAPTURE_PROFILE)


def _kill_capture_chrome() -> None:
    marker = CAPTURE_PROFILE
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.CommandLine -like '*{marker}*' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        timeout=25,
    )
    time.sleep(0.8)
    try:
        from c168_proxy import stop_all_relays

        stop_all_relays()
    except Exception:
        pass


def _wipe_profile() -> None:
    _kill_capture_chrome()
    p = _profile_dir()
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)


def _cdp_alive(cdp_base: str | None = None) -> bool:
    base = (cdp_base or current_cdp_url()).rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/json/version", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _start_chrome(url: str, *, proxy: str = "") -> tuple[bool, str]:
    from c168_proxy import chrome_proxy_server

    chrome = _find_chrome_exe()
    if not chrome:
        return False, "Không tìm thấy chrome.exe"
    cmd = [
        chrome,
        f"--remote-debugging-port={CAPTURE_PORT}",
        "--remote-allow-origins=*",
        f"--user-data-dir={_profile_dir()}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1440,900",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=CalculateNativeWinOcclusion",
    ]
    proxy_srv = chrome_proxy_server(proxy) if proxy.strip() else ""
    if proxy_srv:
        cmd.append(f"--proxy-server={proxy_srv}")
        # CDP vẫn localhost; tránh một số request nội bộ kẹt proxy chết
        cmd.append("--proxy-bypass-list=<-loopback>;127.0.0.1;localhost")
    cmd.append(url)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        return False, str(e)
    # Gắn vòng đời relay vào Chrome capture: Chrome đóng -> guardian kill relay.
    if proxy.strip():
        try:
            from c168_proxy import relay_pid_for, spawn_relay_guardian

            rpid = relay_pid_for(proxy)
            if rpid:
                spawn_relay_guardian(chrome_pid=proc.pid, relay_pid=rpid)
        except Exception:
            pass
    for _ in range(50):
        if _cdp_alive():
            return True, CDP_URL
        time.sleep(0.4)
    return False, f"Port {CAPTURE_PORT} chưa lên sau ~20s"


def _api_path(url: str) -> str:
    p = urlparse(url).path or url
    i = p.lower().find("/hall/api/")
    return p[i:] if i >= 0 else p


def _pick_headers(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    low = {k.lower(): v for k, v in headers.items()}
    for k in SESSION_HEADER_KEYS:
        if k in low:
            v = low[k]
            out[k] = v
    return out


def _parse_json_body(body: str) -> dict[str, Any]:
    raw = (body or "").strip()
    if not raw.startswith("{"):
        return {}
    try:
        return json.loads(raw.split("\n")[0][:50_000])
    except Exception:
        return {}


def analyze_game_b(events: list[dict[str, Any]]) -> dict[str, Any]:
    base = analyze_api_events(events)
    game_logins: list[dict[str, Any]] = []
    game_logouts: list[dict[str, Any]] = []
    vendor_nav: list[dict[str, Any]] = []
    session_headers: dict[str, str] = {}

    for i, ev in enumerate(events):
        if ev.get("kind") == "request" and ev.get("session_headers"):
            session_headers.update(ev["session_headers"])
        path = str(ev.get("path") or "")
        if ev.get("kind") == "request" and GAME_LOGIN_RE.search(path):
            resp = None
            for j in range(i + 1, min(i + 8, len(events))):
                if events[j].get("kind") == "response" and events[j].get("path") == path:
                    resp = events[j]
                    break
            body = str(resp.get("body") or "") if resp else ""
            parsed = _parse_json_body(body)
            game_logins.append(
                {
                    "path": path,
                    "post": ev.get("post_preview"),
                    "x_data_mode": ev.get("x_data_mode"),
                    "request_headers": ev.get("session_headers"),
                    "status": resp.get("status") if resp else None,
                    "response_plain_json": parsed if parsed else None,
                    "response_encrypted": bool(
                        body and not parsed and len(body) > 40
                    ),
                    "response_preview": body[:800] if body else "",
                }
            )
        if ev.get("kind") == "response" and "gameApi/logout" in path:
            game_logouts.append(
                {
                    "path": path,
                    "body_preview": str(ev.get("body") or "")[:500],
                    "parsed": _parse_json_body(str(ev.get("body") or "")),
                }
            )
        url = str(ev.get("url") or "")
        if VENDOR_RE.search(url):
            vendor_nav.append({"kind": ev.get("kind"), "url": url[:200]})

    # Suy luận field POST login
    post_fields: dict[str, Any] = {}
    if game_logins:
        try:
            post_fields = json.loads(str(game_logins[-1].get("post") or "{}"))
        except Exception:
            pass

    return {
        **base,
        "game_b": {
            "session_headers_seen": session_headers,
            "game_api_login_calls": game_logins,
            "game_api_logout_calls": game_logouts,
            "vendor_urls": vendor_nav[-20:],
            "inferred_login_body_fields": post_fields,
            "checklist_no_browser": _checklist(game_logins, session_headers, post_fields),
        },
    }


def _checklist(
    logins: list[dict],
    headers: dict[str, str],
    post_fields: dict[str, Any],
) -> list[str]:
    items = [
        "POST https://<hall>/hall/api/gameCenter/gameApi/login",
        "Headers: token, newjwt, device, browserfingerid, sitecode, domain, timestamp, x-request-id, x-version",
        "Body (plain): os_type, gameid, platfromid, exitUrl, time",
    ]
    if headers.get("token"):
        items.append("✓ Đã thấy token trong capture")
    else:
        items.append("✗ Chưa thấy token — cần đăng nhập trước khi click Game B")
    if logins:
        items.append(f"✓ gameApi/login × {len(logins)} — platfromid={post_fields.get('platfromid')}, gameid={post_fields.get('gameid')}")
        if logins[-1].get("response_plain_json"):
            items.append("✓ Response login JSON đọc được — có thể trích launch URL")
        elif logins[-1].get("response_encrypted"):
            items.append("⚠ Response login mã hóa — cần reverse chipher hoặc giữ session browser")
    else:
        items.append("✗ Chưa bắt gameApi/login — hãy click vào vendor / Game B trước khi tắt Chrome")
    return items


def _fetch_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=8) as r:
        return json.loads(r.read().decode())


def _headers_dict(headers: Any) -> dict[str, str]:
    if isinstance(headers, dict):
        return {str(k): str(v) for k, v in headers.items()}
    out: dict[str, str] = {}
    if isinstance(headers, list):
        for h in headers:
            if isinstance(h, dict):
                out[str(h.get("name", ""))] = str(h.get("value", ""))
    return out


def _should_sniff_tab(url: str) -> bool:
    u = (url or "").lower()
    if not u or u.startswith("devtools://"):
        return False
    if "accounts.google.com/gsi" in u:
        return False
    return any(
        k in u
        for k in ("c1686.net", "c168f.com", "c168b2", "bpcdf.", "tgmeq", "mhuxu", "about:blank")
    )


class _TabNetworkRecorder:
    """Một WebSocket CDP trực tiếp tới từng tab (ổn định hơn browser attach)."""

    def __init__(
        self,
        wss: str,
        page_url: str,
        *,
        on_event: Callable[[dict[str, Any]], None],
        stats: "TabSnifferPool",
    ) -> None:
        self.wss = wss
        self.page_url = page_url
        self.on_event = on_event
        self.stats = stats
        self._ws: Any = None
        self._alive = False
        self._id = 0
        self._responses: dict[int, dict] = {}
        self._pending: dict[str, dict[str, Any]] = {}

    def _send(self, method: str, params: dict | None = None) -> int:
        if not self._alive or not self._ws:
            return 0
        self._id += 1
        mid = self._id
        try:
            self._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        except Exception:
            self._alive = False
        return mid

    def _maybe_log_request(
        self,
        *,
        url: str,
        method: str,
        headers: dict[str, str],
        post_data: str,
        request_id: str,
    ) -> None:
        if not HALL_RE.search(url) and not (
            VENDOR_RE.search(url) and method in ("GET", "POST")
        ):
            return
        path = _api_path(url) if HALL_RE.search(url) else ""
        row: dict[str, Any] = {
            "kind": "request",
            "ts": _ts(),
            "url": url,
            "method": method,
            "path": path,
            "tab": self.page_url[:120],
            "x_data_mode": headers.get("x-data-mode") or headers.get("X-Data-Mode"),
            "post_preview": (post_data or "")[:8000],
        }
        if path and (
            GAME_LOGIN_RE.search(path) or "member/login" in path.lower()
        ):
            row["session_headers"] = _pick_headers(headers)
        if VENDOR_RE.search(url):
            row["vendor"] = True
        self._pending[request_id] = row
        self.stats.network_events += 1
        self.on_event(row)
        if path and GAME_LOGIN_RE.search(path):
            print("  ★ REQUEST gameApi/login", file=sys.stderr)

    def _on_message(self, _w: Any, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if "id" in msg:
            self._responses[msg["id"]] = msg
            mid = msg["id"]
            meta = self._pending.pop(f"body:{mid}", None)
            if meta and "result" in msg:
                body = str((msg.get("result") or {}).get("body") or "")
                if (msg.get("result") or {}).get("base64Encoded"):
                    try:
                        body = base64.b64decode(body).decode("utf-8", errors="replace")
                    except Exception:
                        pass
                path = str(meta.get("path") or "")
                row = {
                    "kind": "response",
                    "ts": _ts(),
                    "status": meta.get("response_status"),
                    "url": meta.get("url"),
                    "path": path,
                    "method": meta.get("method"),
                    "tab": self.page_url[:120],
                    "body": body[:24_000],
                }
                self.on_event(row)
                if path:
                    print(
                        f"  [API] {meta.get('method')} {path} → {meta.get('response_status')}",
                        file=sys.stderr,
                    )
                if GAME_LOGIN_RE.search(path):
                    print(f"  ★ RESPONSE gameApi/login ({len(body)} bytes)", file=sys.stderr)
            return

        method = msg.get("method") or ""
        params = msg.get("params") or {}
        if method == "Network.requestWillBeSent":
            req = params.get("request") or {}
            self._maybe_log_request(
                url=str(req.get("url") or ""),
                method=str(req.get("method") or "GET"),
                headers=_headers_dict(req.get("headers")),
                post_data=str(req.get("postData") or ""),
                request_id=str(params.get("requestId") or ""),
            )
        elif method == "Network.responseReceived":
            rid = str(params.get("requestId") or "")
            resp = params.get("response") or {}
            url = str(resp.get("url") or "")
            if HALL_RE.search(url):
                meta = self._pending.setdefault(
                    rid,
                    {"kind": "request", "path": _api_path(url), "url": url},
                )
                meta["response_status"] = resp.get("status")
        elif method == "Network.loadingFinished":
            rid = str(params.get("requestId") or "")
            meta = self._pending.get(rid)
            if meta and HALL_RE.search(str(meta.get("url") or "")):
                mid = self._send("Network.getResponseBody", {"requestId": rid})
                if mid:
                    self._pending[f"body:{mid}"] = meta

    def start(self) -> bool:
        if ws_lib is None:
            return False
        ok: list[bool] = []

        def _on_open(ws: Any) -> None:
            self._alive = True
            self._send("Network.enable", {"maxTotalBufferSize": 80_000_000})
            ok.append(True)

        def _on_error(_ws: Any, err: Exception) -> None:
            print(f"[CDP] Tab WS lỗi ({self.page_url[:50]}): {err}", file=sys.stderr)

        self._ws = ws_lib.WebSocketApp(
            self.wss,
            on_open=_on_open,
            on_message=self._on_message,
            on_error=_on_error,
        )
        threading.Thread(
            target=lambda: self._ws.run_forever(ping_interval=15, ping_timeout=10),
            daemon=True,
        ).start()
        time.sleep(0.8)
        return bool(ok)

    def stop(self) -> None:
        self._alive = False
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass


class TabSnifferPool:
    """Quét /json/list — mỗi tab c168/game = 1 CDP WS riêng."""

    def __init__(self, cdp_base: str, *, on_event: Callable[[dict[str, Any]], None]) -> None:
        self.cdp_base = cdp_base.rstrip("/")
        self.on_event = on_event
        self._recorders: dict[str, _TabNetworkRecorder] = {}
        self.sessions_enabled = 0
        self.network_events = 0

    def poll_tabs(self) -> None:
        try:
            targets = _fetch_json(f"{self.cdp_base}/json/list")
        except Exception:
            return
        for t in targets:
            wss = str(t.get("webSocketDebuggerUrl") or "")
            if not wss or wss in self._recorders:
                continue
            typ = str(t.get("type") or "")
            if typ not in ("page", "iframe", "service_worker"):
                continue
            url = str(t.get("url") or "")
            if not _should_sniff_tab(url):
                continue
            rec = _TabNetworkRecorder(wss, url, on_event=self.on_event, stats=self)
            if rec.start():
                self._recorders[wss] = rec
                self.sessions_enabled = len(self._recorders)
                print(f"[CDP] Network ON tab: {url[:75]}", file=sys.stderr)

    def start(self) -> bool:
        if ws_lib is None:
            print("pip install websocket-client", file=sys.stderr)
            return False
        try:
            _fetch_json(f"{self.cdp_base}/json/version")
        except Exception as exc:
            print(f"CDP: {exc}", file=sys.stderr)
            return False
        for _ in range(5):
            self.poll_tabs()
            time.sleep(1.0)
        print("[CDP] Sniff từng tab (WS trực tiếp) — không Playwright", file=sys.stderr)
        return True

    def stop(self) -> None:
        for rec in self._recorders.values():
            rec.stop()
        self._recorders.clear()


def print_game_b_summary(analysis: dict[str, Any]) -> None:
    gb = analysis.get("game_b") or {}
    print("\n=== GAME B — CẦN GÌ ĐỂ GỌI API (không browser) ===", file=sys.stderr)
    for line in gb.get("checklist_no_browser") or []:
        print(f"  {line}", file=sys.stderr)
    logins = gb.get("game_api_login_calls") or []
    if logins:
        last = logins[-1]
        print("\n  Login POST mẫu:", file=sys.stderr)
        print(f"    {last.get('post')}", file=sys.stderr)
        print("  Headers session (rút gọn):", file=sys.stderr)
        for k, v in (last.get("request_headers") or {}).items():
            print(f"    {k}: {v}", file=sys.stderr)
        if last.get("response_plain_json"):
            data = last["response_plain_json"].get("data")
            print(f"  Response data keys: {list(data.keys()) if isinstance(data, dict) else data}", file=sys.stderr)
    print("", file=sys.stderr)


def run_capture(*, start_url: str, log_path: Path) -> dict[str, Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")
    events: list[dict[str, Any]] = []

    def on_event(row: dict[str, Any]) -> None:
        events.append(row)
        _log_append(log_path, row)

    result: dict[str, Any] = {
        "ok": False,
        "cdp": CDP_URL,
        "profile": _profile_dir(),
        "log_file": str(log_path),
        "start_url": start_url,
        "proxy": None,
    }

    print("Tắt Chrome capture cũ, tạo profile mới…", file=sys.stderr)
    _wipe_profile()
    print(f"Mở Chrome (KHÔNG proxy) port {CAPTURE_PORT}…", file=sys.stderr)
    ok, msg = _start_chrome(start_url)
    if not ok:
        result["error"] = msg
        return result
    print(f"Chrome: {msg}", file=sys.stderr)

    print(
        f"\n{'═' * 44}\n"
        f"  BẠN LÀM TRÊN CHROME VỪA MỞ (KHÔNG banner Debugger paused):\n"
        f"  1) Đăng ký / đăng nhập\n"
        f"  2) Click SEXY / vendor → vào bàn\n"
        f"  3) TẮT HẲN Chrome capture khi xong\n"
        f"     profile: {CAPTURE_PROFILE}\n"
        f"  Ghi log: {log_path}\n"
        f"{'═' * 44}\n",
        file=sys.stderr,
    )

    sniffer = TabSnifferPool(CDP_URL, on_event=on_event)
    if not sniffer.start():
        result["error"] = "CDP sniff không khởi động"
        return result

    t_start = time.time()
    dead_streak = 0
    last_ping = 0.0
    while True:
        if _cdp_alive():
            dead_streak = 0
            sniffer.poll_tabs()
        else:
            dead_streak += 1
            if dead_streak >= 3:
                print("\nChrome đã đóng — phân tích log…", file=sys.stderr)
                break
        now = time.time()
        if now - last_ping >= 15:
            last_ping = now
            elapsed = int(now - t_start)
            print(
                f"  … {elapsed}s | API log={len(events)} | "
                f"CDP sessions={sniffer.sessions_enabled} | "
                f"net={sniffer.network_events} — tắt Chrome sau khi vào game",
                file=sys.stderr,
            )
            if elapsed >= 60 and len(events) == 0 and sniffer.sessions_enabled == 0:
                print(
                    "  ⚠ Vẫn 0 API — thử F5 trang hoặc mở tab mới (sniffer chưa gắn tab)",
                    file=sys.stderr,
                )
        time.sleep(2)

    sniffer.stop()

    analysis = analyze_game_b(events)
    out_analysis = _DIR / "c168_game_b_analysis.json"
    out_analysis.write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    result["ok"] = True
    result["api_event_count"] = len(events)
    result["analysis_file"] = str(out_analysis)
    result["analysis"] = analysis

    print_analysis_summary(analysis)
    print_game_b_summary(analysis)
    print(f"\nLog: {log_path}", file=sys.stderr)
    print(f"Phân tích: {out_analysis}", file=sys.stderr)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Chrome sạch, ghi API đến khi tắt browser")
    parser.add_argument("--url", default="", help="Trang mở đầu (mặc định c1686.net)")
    parser.add_argument("--log", default="", help="file jsonl")
    args = parser.parse_args()

    cfg = load_config()
    url = (args.url or "https://c1686.net").strip()
    log_path = Path(args.log) if args.log else _DIR / "c168_game_b_capture.jsonl"

    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    out = run_capture(start_url=url, log_path=log_path)
    print(json.dumps({k: v for k, v in out.items() if k != "analysis"}, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
