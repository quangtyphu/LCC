# -*- coding: utf-8 -*-
"""SOCKS5 host:port:user:pass → relay HTTP local cho Chromium/Playwright."""
from __future__ import annotations

import random
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

_relays: dict[str, subprocess.Popen] = {}
_ports: dict[str, int] = {}


def python_executable_no_console() -> str:
    """Windows: pythonw.exe — subprocess không mở cửa sổ cmd đen."""
    if sys.platform != "win32":
        return sys.executable
    pw = Path(sys.executable).with_name("pythonw.exe")
    return str(pw) if pw.is_file() else sys.executable


def subprocess_hide_window_kwargs() -> dict[str, Any]:
    """Windows: không bật cửa sổ cmd khi spawn Python/Chrome/pproxy."""
    if sys.platform != "win32":
        return {}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0x08000000
    flags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return {"creationflags": flags, "startupinfo": si}


def parse_proxy(proxy_str: str) -> tuple[str, int, str, str]:
    """SOCKS5: host:port hoặc host:port:user:pass (pass có thể chứa ':')."""
    s = (proxy_str or "").strip()
    for prefix in ("socks5h://", "socks5://", "http://", "https://"):
        if s.lower().startswith(prefix):
            s = s[len(prefix) :]
    parts = s.split(":")
    if len(parts) >= 4:
        host, port_s, user = parts[0].strip(), parts[1].strip(), parts[2].strip()
        pwd = ":".join(parts[3:]).strip()
        return host, int(port_s), user, pwd
    if len(parts) == 2:
        return parts[0].strip(), int(parts[1]), "", ""
    raise ValueError(
        'proxy SOCKS5: "host:port:user:pass" hoặc "host:port", nhận: ' + repr(proxy_str)
    )


def proxy_has_auth(proxy_str: str) -> bool:
    try:
        _, _, user, _ = parse_proxy(proxy_str)
        return bool(user)
    except ValueError:
        return False


def proxy_label(proxy_str: str) -> str:
    try:
        host, port, user, _ = parse_proxy(proxy_str)
        return f"{host}:{port}" + (f" ({user})" if user else "")
    except Exception:
        s = (proxy_str or "").strip()
        return (s[:40] + "…") if len(s) > 40 else s or "(không proxy)"


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _socks_remote_url(proxy_str: str) -> str:
    """Upstream SOCKS5 cho pproxy (-r). Dùng socks5:// (pproxy không nhận socks5h://)."""
    host, port, user, pwd = parse_proxy(proxy_str)
    if user:
        return f"socks5://{host}:{port}#{user}:{pwd}"
    return f"socks5://{host}:{port}"


def _wait_port(host: str, port: int, timeout: float = 8.0) -> bool:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def ensure_local_http_relay(proxy_str: str) -> str:
    """
    SOCKS5 host:port:user:pass → HTTP 127.0.0.1:PORT (Chrome chỉ gắn được HTTP local).
    Cần: pip install pproxy
    """
    import time

    key = proxy_str.strip()
    proc = _relays.get(key)
    if proc is not None and proc.poll() is None:
        return f"http://127.0.0.1:{_ports[key]}"

    try:
        import pproxy  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "SOCKS5 có user/pass cần relay: chạy pip install pproxy rồi thử lại"
        ) from e

    local_port = _free_port()
    popen_kw: dict[str, Any] = {
        "args": [
            python_executable_no_console(),
            "-m",
            "pproxy",
            "-l",
            f"http://127.0.0.1:{local_port}/",
            "-r",
            _socks_remote_url(key),
        ],
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        flags = subprocess_hide_window_kwargs().get("creationflags", 0)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        popen_kw.update(subprocess_hide_window_kwargs())
        popen_kw["creationflags"] = flags
    else:
        popen_kw["start_new_session"] = True
    proc = subprocess.Popen(**popen_kw)
    if not _wait_port("127.0.0.1", local_port, timeout=8.0):
        err = ""
        if proc.poll() is not None and proc.stderr:
            try:
                err = proc.stderr.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
        try:
            proc.terminate()
        except Exception:
            pass
        raise RuntimeError(
            f"Không khởi động relay SOCKS5→HTTP (port {local_port}). "
            f"{err or 'Kiểm tra proxy host:port:user:pass'}"
        )
    if proc.poll() is not None:
        err = ""
        if proc.stderr:
            try:
                err = proc.stderr.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
        raise RuntimeError(f"Relay pproxy thoát sớm: {err or 'lỗi không rõ'}")
    _relays[key] = proc
    _ports[key] = local_port
    return f"http://127.0.0.1:{local_port}"


def prepare_chrome_proxy(proxy_str: str) -> dict[str, str]:
    """
    Chuẩn bị proxy cho chrome.exe.
    - host:port:user:pass → relay HTTP local (SOCKS5 auth)
    - host:port → socks5:// trực tiếp
    """
    p = (proxy_str or "").strip()
    if not p:
        return {"mode": "none", "server": "", "label": "(không proxy)"}
    host, port, user, _ = parse_proxy(p)
    if user:
        local = ensure_local_http_relay(p)
        return {
            "mode": "socks5_auth_relay",
            "server": local,
            "label": f"SOCKS5 {host}:{port} (+auth) → {local}",
        }
    return {
        "mode": "socks5_direct",
        "server": f"socks5://{host}:{port}",
        "label": f"SOCKS5 {host}:{port}",
    }


def stop_all_relays() -> None:
    for proc in list(_relays.values()):
        if proc.poll() is None:
            proc.terminate()
    _relays.clear()
    _ports.clear()


# Không atexit.stop_all_relays: cms_launch thoát ngay sau khi mở Chrome,
# atexit sẽ giết relay → Chrome mất mạng. Relay tái dùng theo proxy string.


def playwright_proxy(proxy_str: str) -> dict[str, str] | None:
    p = (proxy_str or "").strip()
    if not p:
        return None
    prep = prepare_chrome_proxy(p)
    if not prep.get("server"):
        return None
    return {"server": prep["server"]}


def chrome_proxy_flag(proxy_str: str) -> str:
    """--proxy-server= cho chrome.exe."""
    return prepare_chrome_proxy(proxy_str).get("server") or ""


def pick_from_file(path: Path) -> str:
    lines = [
        ln.strip()
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    if not lines:
        raise RuntimeError(f"File proxy trống: {path}")
    choice = random.choice(lines)
    parse_proxy(choice)
    return choice
