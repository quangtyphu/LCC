# -*- coding: utf-8 -*-
"""Chrome remote debugging cho benbet capture."""
from __future__ import annotations

import os
import subprocess
import time
import urllib.error
import urllib.request

CDP_PORT = 9223
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
PROFILE_DIR = os.path.join(os.environ.get("TEMP", "."), "benbet_chrome_capture")


def find_chrome_exe() -> str:
    candidates = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            "Google",
            "Chrome",
            "Application",
            "chrome.exe",
        ),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return ""


def open_cdp_tab(url: str, *, cdp_base: str = CDP_URL) -> bool:
    """Mo tab moi trong Chrome CDP dang chay."""
    from urllib.parse import quote

    try:
        req = urllib.request.Request(
            f"{cdp_base.rstrip('/')}/json/new?{quote(url, safe='')}",
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 201)
    except Exception:
        return False


def cdp_is_alive(url: str = CDP_URL) -> bool:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/json/version", timeout=2) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def start_chrome(url: str, *, port: int = CDP_PORT) -> tuple[bool, str]:
    chrome = find_chrome_exe()
    if not chrome:
        return False, "Khong tim thay chrome.exe"
    os.makedirs(PROFILE_DIR, exist_ok=True)
    cmd = [
        chrome,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1334,750",
        url,
    ]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        return False, str(exc)
    cdp = f"http://127.0.0.1:{port}"
    for _ in range(50):
        if cdp_is_alive(cdp):
            return True, cdp
        time.sleep(0.4)
    return False, f"Port {port} chua san sang"
