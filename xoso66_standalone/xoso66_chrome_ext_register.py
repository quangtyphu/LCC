# -*- coding: utf-8 -*-
"""
Đăng ký XOSO66 qua Chrome extension — KHÔNG CDP, KHÔNG Playwright.

Chrome mở như CMS (cdp_port=0) + --load-extension → content script gọi Vue store.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from base64 import b64encode
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from xoso66_session import BASE_URL

_EXT_DIR = Path(__file__).resolve().parent / "xoso66_register_ext"
_RESULT: dict[str, Any] = {}
_RESULT_LOCK = threading.Lock()


def _browser_isolate_dir() -> Path:
    root = Path(__file__).resolve().parent.parent / "browser_isolate"
    if not root.is_dir():
        raise RuntimeError("Thiếu browser_isolate")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root


class _CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:
            data = {"ok": False, "error": "invalid_json"}
        with _RESULT_LOCK:
            _RESULT.clear()
            _RESULT.update(data if isinstance(data, dict) else {"raw": data})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def _free_port() -> int:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _start_callback_server(port: int) -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def _launch_chrome_with_extension(
    profile_dir: Path,
    proxy: str,
    url: str,
) -> subprocess.Popen[Any]:
    _browser_isolate_dir()
    from launcher import _find_chrome_exe, _launch_chrome_native  # type: ignore

    chrome = _find_chrome_exe()
    if not chrome:
        raise RuntimeError("Không tìm thấy chrome.exe")
    if not _EXT_DIR.is_dir():
        raise RuntimeError(f"Thiếu extension: {_EXT_DIR}")

    profile_dir.mkdir(parents=True, exist_ok=True)
    ext = str(_EXT_DIR.resolve())
    cmd = [
        chrome,
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        f"--disable-extensions-except={ext}",
        f"--load-extension={ext}",
    ]
    prep = None
    if proxy.strip():
        from proxy_util import prepare_chrome_proxy

        prep = prepare_chrome_proxy(proxy)
        server = prep.get("server") or ""
        if server:
            cmd.append(f"--proxy-server={server}")
    cmd.append(url)
    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )


def register_via_chrome_extension(
    session: dict,
    plain: dict,
    profile_dir: Path,
    *,
    timeout_sec: int = 180,
) -> dict[str, Any]:
    """
    Mở Chrome CMS + extension, gọi user/register trong page context.
    """
    from xoso66_chrome_profile import (
        profile_has_cf_clearance,
        profile_is_locked,
        terminate_chrome,
        wait_cf_clearance_profile,
    )

    proxy = str(session.get("proxy") or "").strip()
    if not proxy:
        return {"ok": False, "error": "missing_proxy"}

    port = _free_port()
    callback = f"http://127.0.0.1:{port}/done"
    srv = _start_callback_server(port)

    payload = dict(plain)
    payload["_callback"] = callback
    b64 = b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
    # Hash giữ payload khi site redirect /home/?… → /home (Vue history mode).
    url = f"{BASE_URL}/home/#xoso66_reg={b64}"

    meta: dict[str, Any] = {"method": "chrome_extension", "cdp": False, "callback_port": port}
    locked = profile_is_locked(profile_dir)
    if locked:
        print(
            "[REGISTER] Profile Chrome đang mở — bỏ qua extension, dùng Playwright.",
            flush=True,
        )
        return {
            "ok": False,
            "error": "profile_in_use",
            "skipped": True,
            **meta,
        }
    print(
        "[REGISTER] Chrome mở với extension — nếu CF chọn con vật, bấm tay trong cửa sổ Chrome.",
        flush=True,
    )
    proc: subprocess.Popen[Any] | None = None
    try:
        if profile_has_cf_clearance(profile_dir):
            meta["skipped_cf_wait"] = True
        proc = _launch_chrome_with_extension(profile_dir, proxy, url)
        meta["pid"] = proc.pid
        if not meta.get("skipped_cf_wait"):
            wait = wait_cf_clearance_profile(profile_dir, proc, timeout_sec=min(120, timeout_sec))
            meta["cf_wait"] = wait
            if not wait.get("ok"):
                return {**meta, "ok": False, "error": wait.get("error") or "cf_clearance_timeout"}

        deadline = time.time() + max(30, int(timeout_sec))
        while time.time() < deadline:
            with _RESULT_LOCK:
                if _RESULT:
                    meta["result"] = dict(_RESULT)
                    return {**meta, **_RESULT}
            if proc.poll() is not None:
                with _RESULT_LOCK:
                    if _RESULT:
                        return {**meta, **_RESULT}
                return {
                    **meta,
                    "ok": False,
                    "error": "chrome_exited",
                    "exit": proc.returncode,
                    "msg": (
                        "Chrome thoát sớm — có thể profile đang bị CMS giữ. "
                        "Đóng Chrome CMS (XMSB17) rồi chạy lại."
                    ),
                }
            time.sleep(0.5)
        return {**meta, "ok": False, "error": "register_timeout"}
    finally:
        terminate_chrome(proc)
        try:
            srv.shutdown()
        except Exception:
            pass
