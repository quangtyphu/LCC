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

from allgame.db.accounts_db import get_account, resolve_profile_dir, update_session, upsert_account
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


def _pids_listening_on_port(port: int) -> list[int]:
    if port < 1:
        return []
    try:
        proc = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            timeout=20,
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        return []
    pids: list[int] = []
    needle = f":{port}"
    for line in (proc.stdout or "").splitlines():
        row = " ".join(line.split())
        if needle not in row:
            continue
        if "LISTENING" not in row.upper():
            continue
        parts = row.split(" ")
        if len(parts) < 5:
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        if pid > 0 and pid not in pids:
            pids.append(pid)
    return pids


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

    launched = False
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
        proc = subprocess.Popen(
            [python, *args],
            cwd=str(launcher.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        launched = True

    for _ in range(32):
        if _cdp_alive(cdp_url):
            focused = focus_or_open_c168_tab(cdp_url) if launched else {"ok": True, "skipped": True}
            return {
                "ok": True,
                "stage": "chrome_ready",
                "cdp_url": cdp_url,
                "focus": focused,
                "profile_dir": profile_dir,
                "launched": launched,
            }
        time.sleep(0.25)
    return {"ok": False, "error": f"cdp_not_ready:{cdp_url}", "stage": "ensure_chrome_running"}


def close_chrome_for_username(username: str, account: dict[str, Any] | None = None) -> dict[str, Any]:
    user = str(username or "").strip()
    acc = account or get_account("c168", user)
    if not user or not acc:
        return {"ok": False, "error": "account_not_found", "username": user}
    try:
        cdp_port = int(acc.get("chrome_cdp_port") or 0)
    except (TypeError, ValueError):
        cdp_port = 0
    if cdp_port < 1:
        cdp_port = _default_cdp_port(user)
    cdp_url = f"http://127.0.0.1:{cdp_port}"
    if not _cdp_alive(cdp_url):
        return {"ok": True, "closed": False, "reason": "already_closed", "cdp_url": cdp_url}
    pids = _pids_listening_on_port(cdp_port)
    if not pids:
        return {
            "ok": False,
            "closed": False,
            "error": "cdp_alive_but_no_pid",
            "cdp_url": cdp_url,
        }
    closed_pids: list[int] = []
    for pid in pids:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=20,
                encoding="utf-8",
                errors="replace",
            )
            closed_pids.append(pid)
        except Exception:
            continue
    time.sleep(0.6)
    return {
        "ok": not _cdp_alive(cdp_url),
        "closed": bool(closed_pids),
        "pids": closed_pids,
        "cdp_url": cdp_url,
    }


def focus_or_open_c168_tab(cdp_url: str) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return {"ok": False, "error": "playwright_not_installed"}
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp_url)
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
    except Exception as e:
        return {"ok": False, "error": f"focus_tab_failed:{e}"}


def _read_session_via_cdp(
    cdp_url: str,
    *,
    poll_sec: float = 4.0,
    goto_timeout_ms: int = 18_000,
) -> dict[str, Any]:
    """Đọc token localStorage — poll ngắn, tránh goto 120s khi Chrome vừa mở."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return {"ok": False, "error": "playwright_not_installed"}
    home = "https://c168b2.cc/"
    deadline = time.time() + max(1.0, float(poll_sec))
    last: dict[str, Any] = {"ok": False, "error": "no_attempt"}
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url.rstrip("/"))
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = None
        for pg in context.pages:
            if "c168" in str(pg.url or "").lower():
                page = pg
                break
        if page is None:
            page = context.pages[0] if context.pages else context.new_page()
        while time.time() < deadline:
            try:
                page.bring_to_front()
            except Exception:
                pass
            if "c168" not in str(page.url or "").lower():
                try:
                    page.goto(home, wait_until="domcontentloaded", timeout=goto_timeout_ms)
                except Exception:
                    pass
                page.wait_for_timeout(350)
            try:
                snap = page.evaluate(_JS_READ_SESSION)
            except Exception as e:
                snap = {"ok": False, "error": str(e)}
            last = snap if isinstance(snap, dict) else {"ok": False, "error": "bad_js_result"}
            if last.get("ok"):
                return last
            page.wait_for_timeout(280)
    return last


def read_and_update_token(
    username: str,
    *,
    chrome: dict[str, Any] | None = None,
) -> dict[str, Any]:
    user = str(username or "").strip()
    acc = get_account("c168", user)
    if not acc:
        return {"ok": False, "error": "account_not_found", "username": user}
    if chrome and chrome.get("ok") and chrome.get("cdp_url"):
        chrome_out = chrome
    else:
        chrome_out = ensure_chrome_for_username(user, account=acc)
    if not chrome_out.get("ok"):
        return chrome_out
    cdp_url = str(chrome_out.get("cdp_url") or "")
    poll = 10.0 if chrome_out.get("launched") else 3.0
    snap = _read_session_via_cdp(cdp_url, poll_sec=poll, goto_timeout_ms=18_000)
    if not isinstance(snap, dict) or not snap.get("ok"):
        return {
            "ok": False,
            "error": str(snap.get("error") if isinstance(snap, dict) else "bad_js_result"),
            "chrome": chrome_out,
        }
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
    # Một số lúc API trả code tạm thời (vd 2025) ngay sau khi vừa mở tab.
    # Thử đọc/lưu token và check lại 1 lần để tránh skip oan account.
    if not bool(alive.get("ok")) and str(alive.get("code") or "") in {"2025", "1401", "41001401"}:
        time.sleep(0.35)
        snap2 = _read_session_via_cdp(cdp_url, poll_sec=6.0, goto_timeout_ms=15_000)
        if isinstance(snap2, dict) and snap2.get("ok"):
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
                patch2 = {k: snap2.get(k) for k in keep_keys if snap2.get(k)}
                update_session("c168", user, patch2)
                acc2 = get_account("c168", user) or acc2
                alive = checker.read_token_snapshot(acc2)
    bal_raw = alive.get("balance")
    balance_updated = False
    balance_value: float | None = None
    if bal_raw is not None:
        try:
            balance_value = float(str(bal_raw).replace(",", "").strip())
        except (TypeError, ValueError):
            balance_value = None
    if bool(alive.get("ok")) and balance_value is not None:
        upsert_account(
            {
                "portal_id": "c168",
                "username": user,
                "balance": balance_value,
            }
        )
        balance_updated = True
    return {
        "ok": bool(alive.get("ok")),
        "username": user,
        "chrome": chrome_out,
        "token_snapshot": alive,
        "balance_value": balance_value,
        "balance_updated": balance_updated,
    }


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

