# -*- coding: utf-8 -*-
"""
Relay HTTP local → SOCKS5 (có user/pass) cho Playwright/Chromium.

Chromium không hỗ trợ SOCKS5 auth; pproxy lắng nghe 127.0.0.1 rồi forward qua SOCKS5.

  pip install pproxy
"""

from __future__ import annotations

import atexit
import socket
import subprocess
import sys
import time
from typing import Callable

from xoso66_proxy import parse_proxy, proxy_has_auth

_relays: dict[str, subprocess.Popen] = {}
_ports: dict[str, int] = {}


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _socks_remote_url(proxy_str: str) -> str:
    """URI remote cho pproxy (-r). Auth: socks5://host:port#user:pass"""
    host, port, user, pwd = parse_proxy(proxy_str)
    if user:
        return f"socks5://{host}:{port}#{user}:{pwd}"
    return f"socks5://{host}:{port}"


def ensure_local_http_relay(proxy_str: str) -> str:
    """
    Trả URL HTTP proxy local (http://127.0.0.1:PORT) forward qua SOCKS5.
    Process pproxy được cache theo proxy_str.
    """
    key = proxy_str.strip()
    proc = _relays.get(key)
    if proc is not None and proc.poll() is None:
        return f"http://127.0.0.1:{_ports[key]}"

    try:
        import pproxy  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "Cần pproxy cho proxy SOCKS5 có user/pass + Playwright: pip install pproxy"
        ) from e

    local_port = _free_port()
    remote = _socks_remote_url(key)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pproxy",
            "-l",
            f"http://127.0.0.1:{local_port}/",
            "-r",
            remote,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    time.sleep(1.5)
    if proc.poll() is not None:
        err = ""
        if proc.stderr:
            try:
                err = proc.stderr.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
        raise RuntimeError(f"Không khởi động relay local (pproxy): {err or 'exit sớm'}")

    _relays[key] = proc
    _ports[key] = local_port
    return f"http://127.0.0.1:{local_port}"


def stop_all_relays() -> None:
    for proc in list(_relays.values()):
        if proc.poll() is None:
            proc.terminate()
    _relays.clear()
    _ports.clear()


atexit.register(stop_all_relays)


def playwright_proxy_with_relay(proxy_str: str) -> dict[str, str]:
    """Proxy dict cho Playwright — tự bật relay nếu SOCKS5 có auth."""
    if proxy_has_auth(proxy_str):
        return {"server": ensure_local_http_relay(proxy_str)}
    host, port, _, _ = parse_proxy(proxy_str)
    return {"server": f"socks5://{host}:{port}"}
