#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mở Chrome C168 theo username và kiểm tra token còn sống."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import zlib
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent
_REPO = _ROOT.parent.parent.parent
if str(_ROOT) in sys.path:
    sys.path.remove(str(_ROOT))
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from allgame.db.accounts_db import get_account, resolve_profile_dir, update_session
from allgame.portals.c168.token import C168TokenChecker

_JS_READ_SESSION = """
() => {
  try {
    const raw = localStorage.getItem("web__lobby__persisted__token");
    if (!raw) return { ok: false, error: "no_token_in_local_storage" };
    const tok = JSON.parse(decodeURIComponent(raw));
    const info = (tok && tok.tokenInfos) || {};
    const sessionKey = String(info.session_key || "");
    const jwt = String(info.jwt_token || "");
    let device = "";
    try {
      const dRaw = localStorage.getItem("web__lobby__persisted__device");
      device = dRaw ? String((JSON.parse(decodeURIComponent(dRaw)) || {}).uuid || "") : "";
    } catch (e) {}
    return {
      ok: !!(sessionKey && jwt.length > 40),
      session_key: sessionKey,
      newjwt: jwt,
      device: device,
      browserfingerid: String(info.browserfingerid || ""),
      domain: location.host || "",
      origin: location.origin || "",
      appversion: "v7.3.17",
      "x-version": "7.3.17",
      sitecode: String(info.sitecode || "2865"),
      x_device: "1-1",
      error: (!sessionKey || jwt.length <= 40) ? "missing_session_key_or_jwt" : "",
    };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}
"""


def _default_cdp_port(username: str) -> int:
    key = str(username or "").strip().lower().encode("utf-8")
    if not key:
        return 10260
    h = zlib.adler32(key) & 0x7FFF
    return 9361 + (h % 399)


def _cdp_alive(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/json/version", timeout=2) as resp:
            return int(resp.status or 0) == 200
    except Exception:
        return False


def ensure_chrome_for_username(username: str, account: dict[str, Any] | None = None) -> dict[str, Any]:
    user = str(username or "").strip()
    acc = account or get_account("c168", user)
    if not user or not acc:
        return {"ok": False, "error": "account_not_found", "username": user}
    profile_dir = resolve_profile_dir(
        portal_id="c168",
        username=user,
        stored_dir=str(acc.get("chrome_browser_dir") or ""),
    )
    proxy = str(acc.get("proxy") or "").strip()
    if not proxy:
        return {"ok": False, "error": "missing_proxy_for_account", "stage": "ensure_chrome_running"}
    try:
        cdp_port = int(acc.get("chrome_cdp_port") or 0)
    except (TypeError, ValueError):
        cdp_port = 0
    if cdp_port < 1:
        cdp_port = _default_cdp_port(user)
    cdp_url = f"http://127.0.0.1:{cdp_port}"

    launcher = _REPO / "browser_isolate" / "cms_launch.py"
    if not launcher.is_file():
        return {"ok": False, "error": f"missing_launcher:{launcher}", "stage": "ensure_chrome_running"}
    Path(profile_dir).mkdir(parents=True, exist_ok=True)

    if not _cdp_alive(cdp_url):
        python = os.environ.get("PYTHON") or sys.executable
        args = [
            str(launcher),
            "--profile-dir",
            str(profile_dir),
            "--proxy",
            proxy,
            "--cdp-port",
            str(cdp_port),
            "--urls-json",
            json.dumps(["https://c168b2.cc/"]),
        ]
        proc = subprocess.run(
            [python, *args],
            cwd=str(launcher.parent),
            capture_output=True,
            text=True,
            timeout=90,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0:
            return {
                "ok": False,
                "error": f"launcher_exit_{proc.returncode}",
                "stderr": (proc.stderr or "").strip()[:500],
                "stage": "ensure_chrome_running",
            }

    for _ in range(60):
        if _cdp_alive(cdp_url):
            focused = focus_or_open_c168_tab(cdp_url)
            return {
                "ok": True,
                "stage": "chrome_ready",
                "cdp_url": cdp_url,
                "focus": focused,
                "profile_dir": profile_dir,
            }
        time.sleep(0.4)
    return {"ok": False, "error": f"cdp_not_ready:{cdp_url}", "stage": "ensure_chrome_running"}


def focus_or_open_c168_tab(cdp_url: str) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return {"ok": False, "error": "playwright_not_installed"}
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp_url)
            try:
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = None
                for p0 in context.pages:
                    if "c168" in str(p0.url or "").lower():
                        page = p0
                        break
                if page is None:
                    page = context.new_page()
                    page.goto("https://c168b2.cc/", wait_until="domcontentloaded", timeout=120_000)
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                return {"ok": True, "url": str(page.url or "")}
            finally:
                browser.close()
    except Exception as e:
        return {"ok": False, "error": f"focus_tab_failed:{e}"}


def read_and_update_token(username: str) -> dict[str, Any]:
    user = str(username or "").strip()
    acc = get_account("c168", user)
    if not acc:
        return {"ok": False, "error": "account_not_found", "username": user}
    chrome = ensure_chrome_for_username(user, account=acc)
    if not chrome.get("ok"):
        return chrome
    cdp_url = str(chrome.get("cdp_url") or "")
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return {"ok": False, "error": "playwright_not_installed"}
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        try:
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            if "c168" not in str(page.url or "").lower():
                page.goto("https://c168b2.cc/", wait_until="domcontentloaded", timeout=120_000)
                page.wait_for_timeout(1200)
            snap = page.evaluate(_JS_READ_SESSION)
        finally:
            browser.close()
    if not isinstance(snap, dict) or not snap.get("ok"):
        return {"ok": False, "error": str((snap or {}).get("error") if isinstance(snap, dict) else "bad_js_result"), "chrome": chrome}
    keep_keys = (
        "session_key",
        "newjwt",
        "device",
        "browserfingerid",
        "domain",
        "origin",
        "appversion",
        "x-version",
        "sitecode",
        "x_device",
    )
    session_patch = {k: snap.get(k) for k in keep_keys if snap.get(k)}
    update_session("c168", user, session_patch)
    checker = C168TokenChecker()
    acc2 = get_account("c168", user) or acc
    alive = checker.read_token_snapshot(acc2)
    return {"ok": bool(alive.get("ok")), "username": user, "chrome": chrome, "token_snapshot": alive}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="Mở Chrome C168 + đọc token sống")
    parser.add_argument("--username", required=True)
    args = parser.parse_args()
    out = read_and_update_token(args.username)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

