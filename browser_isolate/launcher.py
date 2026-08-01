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
from proxy_util import (
    chrome_proxy_flag,
    playwright_proxy,
    proxy_label,
    subprocess_hide_window_kwargs,
)
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


def _normalize_urls(urls: list[str] | None, fallback: str = "") -> list[str]:
    out: list[str] = []
    for raw in urls or []:
        u = (raw or "").strip()
        if not u or u.startswith("#"):
            continue
        if not u.startswith(("http://", "https://")):
            u = "https://" + u
        out.append(u)
    if not out and fallback.strip():
        fb = fallback.strip()
        if not fb.startswith(("http://", "https://")) and fb != "about:blank":
            fb = "https://" + fb
        out.append(fb)
    return out


def _launch_chrome_native(
    *,
    profile_path: Path,
    proxy: str,
    url: str = "",
    urls: list[str] | None = None,
    cdp_port: int = 0,
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
        "--new-window",
    ]
    port = int(cdp_port or 0)
    if port > 0:
        cmd.extend(
            [
                f"--remote-debugging-port={port}",
                "--remote-allow-origins=*",
                "--window-size=1440,900",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ]
        )
    prep = None
    if proxy.strip():
        from proxy_util import prepare_chrome_proxy

        prep = prepare_chrome_proxy(proxy)
        server = prep.get("server") or ""
        if server:
            cmd.append(f"--proxy-server={server}")
    open_urls = _normalize_urls(urls, url or "about:blank")
    if not open_urls:
        open_urls = ["about:blank"]
    cmd.extend(open_urls)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )

    # Gắn vòng đời relay pproxy vào Chrome: Chrome đóng -> guardian kill relay.
    # Tránh relay mồ côi khi cms_launch mở Chrome detached rồi thoát ngay.
    if prep and prep.get("mode") == "socks5_auth_relay" and proxy.strip():
        try:
            from proxy_util import relay_pid_for, spawn_relay_guardian

            rpid = relay_pid_for(proxy)
            if rpid:
                spawn_relay_guardian(chrome_pid=proc.pid, relay_pid=rpid)
        except Exception:
            pass

    return proc


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

    # Chrome Playwright đã đóng -> dọn relay pproxy của phiên này (tiến trình này
    # giữ tham chiếu relay, không sợ giết nhầm Chrome khác).
    try:
        from proxy_util import stop_all_relays

        stop_all_relays()
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
