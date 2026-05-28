# -*- coding: utf-8 -*-
"""Chrome riêng từng acc C168 — lưu trong CMS/game_data/c168_browsers/{username}."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from c168_accounts_db import get_account, resolve_profile_dir, safe_dir_key

_DIR = Path(__file__).resolve().parent
_LC79 = _DIR.parent
_CMS = _LC79.parent / "CMS"
_DATA = Path(os.environ.get("C168_DATA_DIR", _CMS / "game_data"))
BROWSERS_DIR = Path(os.environ.get("C168_BROWSERS_DIR", _DATA / "c168_browsers"))
CMS_LAUNCH = Path(
    os.environ.get("CMS_LAUNCH_SCRIPT", _LC79 / "browser_isolate" / "cms_launch.py")
)
CDP_PORT_BASE = int(os.environ.get("C168_CDP_PORT_BASE", "9360"))
SITE_LOGIN_URL = os.environ.get("C168_LOGIN_URL", "https://c1686.net")


@dataclass
class ChromeSession:
    username: str
    profile_dir: str
    cdp_port: int
    cdp_url: str
    proxy: str

    @property
    def cms(self) -> bool:
        return True


def cdp_port_for_username(username: str) -> int:
    key = str(username or "").strip().lower().encode("utf-8")
    if not key:
        return CDP_PORT_BASE + 900
    h = zlib.adler32(key) & 0x7FFF
    return CDP_PORT_BASE + 1 + (h % 399)


def browser_dir_for_username(username: str) -> Path:
    return BROWSERS_DIR / safe_dir_key(username)


def _cdp_alive(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/json/version", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def chrome_session_from_override(
    *,
    username: str,
    profile_dir: str,
    cdp_port: int = 0,
    proxy: str = "",
) -> ChromeSession | None:
    """AllGame / CMS: CDP + profile không lấy từ c168.db."""
    user = str(username or "").strip()
    pdir = str(profile_dir or "").strip()
    if not user or not pdir:
        return None
    try:
        port = int(cdp_port or 0)
    except (TypeError, ValueError):
        port = 0
    if port < 1:
        port = cdp_port_for_username(user)
    return ChromeSession(
        username=user,
        profile_dir=pdir,
        cdp_port=port,
        cdp_url=f"http://127.0.0.1:{port}",
        proxy=str(proxy or "").strip(),
    )


def resolve_chrome_session(*, username: str = "") -> ChromeSession | None:
    user = str(username or "").strip()
    if not user:
        return None
    acc = get_account(user)
    if not acc:
        return None
    browser_dir = resolve_profile_dir(
        username=user,
        stored_dir=str(acc.get("chrome_browser_dir") or ""),
    )
    try:
        port = int(acc.get("chrome_cdp_port") or 0)
    except (TypeError, ValueError):
        port = 0
    if port < 1:
        port = cdp_port_for_username(user)
    proxy = str(acc.get("proxy") or "").strip()
    return ChromeSession(
        username=str(acc.get("username") or user),
        profile_dir=browser_dir,
        cdp_port=port,
        cdp_url=f"http://127.0.0.1:{port}",
        proxy=proxy,
    )


def ensure_chrome_running(
    session: ChromeSession,
    url: str = SITE_LOGIN_URL,
    *,
    proxy: str = "",
) -> tuple[bool, str]:
    base = session.cdp_url
    if _cdp_alive(base):
        return True, base
    if not CMS_LAUNCH.is_file():
        return False, f"Không thấy {CMS_LAUNCH}"
    px = (proxy or session.proxy or "").strip()
    if not px:
        return False, "Bắt buộc proxy SOCKS5 (host:port:user:pass) — acc C168 chưa có proxy"
    profile_dir = Path(session.profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    BROWSERS_DIR.mkdir(parents=True, exist_ok=True)
    _iso = _LC79 / "browser_isolate"
    if str(_iso) not in sys.path:
        sys.path.insert(0, str(_iso))
    python = os.environ.get("PYTHON") or sys.executable
    args = [
        str(CMS_LAUNCH),
        "--profile-dir",
        str(profile_dir),
        "--proxy",
        px,
        "--cdp-port",
        str(session.cdp_port),
        "--urls-json",
        json.dumps([url or SITE_LOGIN_URL]),
    ]
    try:
        proc = subprocess.run(
            [python, *args],
            cwd=str(CMS_LAUNCH.parent),
            capture_output=True,
            text=True,
            timeout=90,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as e:
        return False, str(e)
    line = (proc.stdout or "").strip().split("\n")[-1] if proc.stdout else ""
    try:
        data = json.loads(line or "{}")
    except json.JSONDecodeError:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        return False, err
    if not data.get("ok"):
        return False, str(data.get("error") or "cms_launch thất bại")
    for _ in range(50):
        if _cdp_alive(base):
            return True, base
        time.sleep(0.4)
    return False, f"CDP {session.cdp_port} chưa lên"
