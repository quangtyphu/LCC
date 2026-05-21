# -*- coding: utf-8 -*-
"""
Proxy SOCKS5 LC79 — format host:port:user:pass (bảng user_profiles, game_data.db).

Playwright/Chromium không auth SOCKS5 trực tiếp → relay HTTP local (pproxy).
"""
from __future__ import annotations

import atexit
import os
import random
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path(
    os.environ.get("LC79_GAME_DATA_DB", r"C:\Users\Quang\Documents\CMS\game_data.db")
)

_relays: dict[str, subprocess.Popen] = {}
_ports: dict[str, int] = {}


def parse_proxy(proxy_str: str) -> tuple[str, int, str, str]:
    s = (proxy_str or "").strip()
    for prefix in ("socks5h://", "socks5://", "http://", "https://"):
        if s.lower().startswith(prefix):
            s = s[len(prefix) :]
    parts = s.split(":")
    if len(parts) == 4:
        host, port_s, user, pwd = parts
        return host.strip(), int(port_s), user.strip(), pwd.strip()
    if len(parts) == 2:
        return parts[0].strip(), int(parts[1]), "", ""
    raise ValueError(
        'proxy phải "host:port:user:pass" hoặc "host:port", nhận: ' + repr(proxy_str)
    )


def proxy_has_auth(proxy_str: str) -> bool:
    try:
        _, _, user, _ = parse_proxy(proxy_str)
        return bool(user)
    except ValueError:
        return False


def proxy_log_label(proxy_str: str) -> str:
    try:
        host, port, user, _ = parse_proxy(proxy_str)
        if user:
            return f"{host}:{port} (socks5, user {user})"
        return f"{host}:{port} (socks5)"
    except Exception:
        s = (proxy_str or "").strip()
        return (s[:48] + "…") if len(s) > 48 else s or "(không có proxy)"


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _socks_remote_url(proxy_str: str) -> str:
    host, port, user, pwd = parse_proxy(proxy_str)
    if user:
        return f"socks5://{host}:{port}#{user}:{pwd}"
    return f"socks5://{host}:{port}"


def ensure_local_http_relay(proxy_str: str) -> str:
    key = proxy_str.strip()
    proc = _relays.get(key)
    if proc is not None and proc.poll() is None:
        return f"http://127.0.0.1:{_ports[key]}"

    try:
        import pproxy  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "Cần pproxy cho SOCKS5 có user/pass + Playwright: pip install pproxy"
        ) from e

    local_port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pproxy",
            "-l",
            f"http://127.0.0.1:{local_port}/",
            "-r",
            _socks_remote_url(key),
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
        raise RuntimeError(f"Không khởi động relay pproxy: {err or 'exit sớm'}")

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


def playwright_proxy_dict(proxy_str: str) -> dict[str, str]:
    """Dict cho browser.new_context(proxy=...)."""
    if proxy_has_auth(proxy_str):
        return {"server": ensure_local_http_relay(proxy_str)}
    host, port, _, _ = parse_proxy(proxy_str)
    return {"server": f"socks5://{host}:{port}"}


def list_proxies_prioritized_from_lc79_db(db_path: str | Path | None = None) -> list[str]:
    """
    Proxy từ user_profiles — ưu tiên acc status 'Đang Chơi' (đang dùng ổn trên LC79).
    """
    path = Path(db_path or DEFAULT_DB_PATH)
    if not path.is_file():
        raise FileNotFoundError(f"Không thấy DB LC79: {path}")
    active: list[str] = []
    other: list[str] = []
    conn = sqlite3.connect(str(path))
    try:
        cur = conn.execute(
            """
            SELECT TRIM(proxy) AS p, TRIM(COALESCE(status, '')) AS st
            FROM user_profiles
            WHERE proxy IS NOT NULL AND TRIM(proxy) != ''
            """
        )
        for p, st in cur.fetchall():
            s = str(p or "").strip()
            if not s:
                continue
            try:
                parse_proxy(s)
            except ValueError:
                continue
            if str(st) == "Đang Chơi":
                active.append(s)
            else:
                other.append(s)
    finally:
        conn.close()
    random.shuffle(active)
    random.shuffle(other)
    seen: set[str] = set()
    out: list[str] = []
    for s in active + other:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def list_proxies_from_lc79_db(db_path: str | Path | None = None) -> list[str]:
    path = Path(db_path or DEFAULT_DB_PATH)
    if not path.is_file():
        raise FileNotFoundError(f"Không thấy DB LC79: {path}")
    conn = sqlite3.connect(str(path))
    try:
        cur = conn.execute(
            """
            SELECT DISTINCT TRIM(proxy) AS p
            FROM user_profiles
            WHERE proxy IS NOT NULL AND TRIM(proxy) != ''
            """
        )
        out: list[str] = []
        for (p,) in cur.fetchall():
            s = str(p or "").strip()
            if not s:
                continue
            try:
                parse_proxy(s)
                out.append(s)
            except ValueError:
                continue
        return out
    finally:
        conn.close()


def pick_random_proxy_from_db(db_path: str | Path | None = None) -> str:
    proxies = list_proxies_prioritized_from_lc79_db(db_path)
    if not proxies:
        raise RuntimeError("DB LC79 không có proxy SOCKS5 nào trong user_profiles")
    return random.choice(proxies)


def resolve_register_proxy(
    *,
    explicit: str = "",
    cfg: dict[str, Any] | None = None,
    use_db: bool = True,
) -> str:
    p = (explicit or "").strip()
    if p:
        parse_proxy(p)
        return p
    proxy_cfg = (cfg or {}).get("proxy") if isinstance((cfg or {}).get("proxy"), dict) else {}
    if proxy_cfg.get("enabled") is False:
        return ""
    if not use_db:
        return ""
    db_path = proxy_cfg.get("db_path") or DEFAULT_DB_PATH
    return pick_random_proxy_from_db(db_path)
