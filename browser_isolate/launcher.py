# -*- coding: utf-8 -*-
"""
Mỗi session = 1 profile Chrome riêng (cookie/storage tách) + 1 proxy.

Lưu ý: Chrome gốc không gán proxy theo từng tab trong cùng cửa sổ.
Muốn nhiều proxy → mở nhiều session (nhiều cửa sổ), giống nhiều profile GoLogin.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
from pathlib import Path
from typing import Any

from fingerprint import random_fingerprint
from proxy_util import chrome_proxy_flag, playwright_proxy, proxy_label
from session_store import (
    delete_session,
    get_session,
    new_session_id,
    profile_dir,
    register_session,
    wipe_profile,
)

_DIR = Path(__file__).resolve().parent


def _find_chrome_exe() -> str:
    candidates = [
        os.path.join(
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            "Google",
            "Chrome",
            "Application",
            "chrome.exe",
        ),
        os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            "Google",
            "Chrome",
            "Application",
            "chrome.exe",
        ),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return ""


def _launch_chrome_native(
    *,
    profile_path: Path,
    proxy: str,
    url: str,
) -> subprocess.Popen[Any]:
    chrome = _find_chrome_exe()
    if not chrome:
        raise RuntimeError("Không tìm thấy chrome.exe")

    profile_path.mkdir(parents=True, exist_ok=True)
    cmd = [
        chrome,
        f"--user-data-dir={profile_path}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
    ]
    px = chrome_proxy_flag(proxy)
    if px:
        cmd.append(f"--proxy-server={px}")
    if url:
        cmd.append(url)

    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def run_playwright_session(
    *,
    session_id: str,
    proxy: str,
    url: str,
    ephemeral: bool,
    reuse_profile: bool,
) -> dict[str, Any]:
    if sys.platform.startswith("win"):
        import asyncio

        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass

    from playwright.sync_api import sync_playwright

    pdir = profile_dir(session_id)
    if not reuse_profile and pdir.is_dir():
        shutil.rmtree(pdir, ignore_errors=True)
    pdir.mkdir(parents=True, exist_ok=True)

    fp = random_fingerprint()
    px = playwright_proxy(proxy)
    launch_kw: dict[str, Any] = {
        "user_data_dir": str(pdir),
        "headless": False,
        "locale": fp["locale"],
        "timezone_id": fp["timezone_id"],
        "viewport": fp["viewport"],
        "user_agent": fp["user_agent"],
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        "ignore_default_args": ["--enable-automation"],
    }
    if px:
        launch_kw["proxy"] = px

    meta = {
        "ok": True,
        "session_id": session_id,
        "mode": "playwright",
        "proxy": proxy_label(proxy),
        "profile_dir": str(pdir),
        "ephemeral": ephemeral,
        "fingerprint": fp,
    }

    with sync_playwright() as p:
        context = None
        for channel in ("chrome", "msedge", None):
            try:
                kw = dict(launch_kw)
                if channel:
                    kw["channel"] = channel
                context = p.chromium.launch_persistent_context(**kw)
                break
            except Exception:
                continue
        if context is None:
            return {"ok": False, "error": "Không launch được Chrome (cài Playwright: playwright install chrome)"}

        if url:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=120_000)

        print(
            f"\n[browser_isolate] Session {session_id} | proxy: {meta['proxy']}\n"
            f"  Profile: {pdir}\n"
            "  Đóng cửa sổ Chrome để kết thúc.\n",
            file=sys.stderr,
        )
        try:
            while context.pages:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            try:
                context.close()
            except Exception:
                pass

    if ephemeral:
        wipe_profile(session_id)
        delete_session(session_id)

    return meta


def run_native_chrome_session(
    *,
    session_id: str,
    proxy: str,
    url: str,
    ephemeral: bool,
    reuse_profile: bool,
) -> dict[str, Any]:
    pdir = profile_dir(session_id)
    if not reuse_profile and pdir.is_dir():
        shutil.rmtree(pdir, ignore_errors=True)

    proc = _launch_chrome_native(profile_path=pdir, proxy=proxy, url=url)
    meta = {
        "ok": True,
        "session_id": session_id,
        "mode": "chrome",
        "proxy": proxy_label(proxy),
        "profile_dir": str(pdir),
        "ephemeral": ephemeral,
        "pid": proc.pid,
    }
    print(
        f"\n[browser_isolate] Chrome native | session {session_id} | proxy: {meta['proxy']}\n"
        f"  Profile: {pdir}\n"
        "  Tắt cửa sổ Chrome (hoặc Ctrl+C) để kết thúc.\n",
        file=sys.stderr,
    )
    try:
        while proc.poll() is None:
            time.sleep(0.5)
    except KeyboardInterrupt:
        proc.terminate()
    if ephemeral:
        wipe_profile(session_id)
        delete_session(session_id)
    return meta


def open_session(
    *,
    proxy: str = "",
    url: str = "about:blank",
    ephemeral: bool = False,
    session_id: str = "",
    reuse: bool = False,
    engine: str = "playwright",
) -> dict[str, Any]:
    sid = session_id or new_session_id()
    if reuse:
        row = get_session(sid)
        if not row:
            return {"ok": False, "error": f"Không có session {sid} trong registry"}
        proxy = proxy or str(row.get("proxy") or "")
    elif not ephemeral:
        register_session(session_id=sid, proxy=proxy, ephemeral=False)

    if engine == "chrome":
        return run_native_chrome_session(
            session_id=sid,
            proxy=proxy,
            url=url,
            ephemeral=ephemeral,
            reuse_profile=reuse,
        )
    return run_playwright_session(
        session_id=sid,
        proxy=proxy,
        url=url,
        ephemeral=ephemeral,
        reuse_profile=reuse,
    )
