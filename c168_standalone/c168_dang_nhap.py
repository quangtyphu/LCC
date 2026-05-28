# -*- coding: utf-8 -*-
"""
C168 — Đăng nhập qua proxy (Chrome CDP).

Chạy không tham số → nhập username, mật khẩu, proxy trong terminal.
"""
from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from c168_config_util import load_config
from c168_proxy import parse_proxy, proxy_log_label
from c168_register import (
    CDP_DEFAULT_URL,
    _apply_mobile_viewport,
    _bind_manual_cdp_browser,
    _close_browser,
    _dismiss_blocking_popups,
    _log_append,
    _pause,
    _safe_fill_input,
    _start_chrome_debug,
    _wipe_c168_chrome_profile,
)

_DIR = Path(__file__).resolve().parent
HALL_API_RE = re.compile(r"/hall/api/", re.I)


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _api_path(url: str) -> str:
    try:
        p = urlparse(url).path or url
        i = p.lower().find("/hall/api/")
        return p[i:] if i >= 0 else p
    except Exception:
        return url


def _ensure_login_tab(page) -> None:
    for label in ("ĐĂNG NHẬP", "Đăng nhập", "DANG NHAP"):
        try:
            tab = page.get_by_text(label, exact=False).first
            if tab.count() and tab.is_visible():
                tab.click(timeout=5000)
                page.wait_for_timeout(500)
                return
        except Exception:
            continue


def _open_login_dialog(page, base: str) -> None:
    """Mở trang đăng nhập chính thức: /home/login (c1686.net)."""
    page.goto(
        base.rstrip("/") + "/home/login",
        wait_until="domcontentloaded",
        timeout=120_000,
    )
    page.wait_for_timeout(2000)
    _dismiss_blocking_popups(page)
    if page.locator("input[type='password']:visible").count() < 1:
        _ensure_login_tab(page)
    if page.locator("input[type='password']:visible").count() < 1:
        for sel in ("[class*='un-login']", "[class*='_un-login']"):
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=5000)
                    page.wait_for_timeout(1200)
                    break
            except Exception:
                continue
        if page.locator("input[type='password']:visible").count() < 1:
            page.goto(
                base.rstrip("/") + "/home/login",
                wait_until="domcontentloaded",
                timeout=120_000,
            )
            page.wait_for_timeout(1500)
            _dismiss_blocking_popups(page)


def _fill_login_form(page, username: str, password: str) -> dict[str, bool]:
    filled = {"username": False, "password": False}
    if page.locator("input[type='password']:visible").count() < 1:
        _ensure_login_tab(page)
    page.wait_for_timeout(400)

    for i in range(page.locator("input:visible").count()):
        if filled["username"] and filled["password"]:
            break
        el = page.locator("input:visible").nth(i)
        try:
            ph = (el.get_attribute("placeholder") or "").strip().lower()
            typ = (el.get_attribute("type") or "text").lower()
            name = (el.get_attribute("data-input-name") or "").strip().lower()
        except Exception:
            continue
        if (
            not filled["username"]
            and (
                "tên tài khoản" in ph
                or "tai khoan" in ph
                or name in ("account", "username", "userpass")
                and typ == "text"
            )
        ):
            _safe_fill_input(el, username)
            filled["username"] = True
        elif (
            not filled["password"]
            and (typ == "password" or "mật khẩu" in ph or name == "userpass")
        ):
            _safe_fill_input(el, password)
            filled["password"] = True

    if not filled["username"]:
        for sel in (
            'input[data-input-name="account"]:visible',
            'section[data-item-name="account"] input.ui-input__input:visible',
            'input[placeholder*="tài khoản" i]:visible',
            "input[type='text']:visible",
        ):
            loc = page.locator(sel).first
            if loc.count():
                _safe_fill_input(loc, username)
                filled["username"] = True
                break
    if not filled["password"]:
        for sel in (
            'input[data-input-name="userpass"]:visible',
            'input[autocomplete="current-password"]:visible',
            "input[type='password']:visible",
            'input[placeholder*="mật khẩu" i]:visible',
        ):
            loc = page.locator(sel).first
            if loc.count():
                _safe_fill_input(loc, password)
                filled["password"] = True
                break
    return filled


def _click_login_submit(page) -> None:
    for sel in (
        "button:has-text('ĐĂNG NHẬP')",
        "button:has-text('Đăng nhập')",
        "[class*='login'] button:visible",
    ):
        try:
            loc = page.locator(sel).last
            if loc.count():
                loc.click(timeout=8000, force=True)
                return
        except Exception:
            continue
    loc = page.get_by_role("button", name=re.compile(r"đăng nhập", re.I)).last
    if loc.count():
        loc.click(timeout=8000, force=True)
        return
    raise RuntimeError("Không tìm thấy nút ĐĂNG NHẬP")


def _detect_logged_in(page) -> bool:
    try:
        text = (
            page.evaluate("() => (document.body && document.body.innerText) || ''") or ""
        ).lower()
        if any(x in text for x in ("đăng xuất", "dang xuat", "số dư", "so du", "nạp tiền")):
            return True
    except Exception:
        pass
    return False


def login(
    *,
    username: str,
    password: str,
    proxy: str,
    cfg: dict[str, Any] | None = None,
    keep_browser_open: bool = False,
) -> dict[str, Any]:
    cfg = cfg or load_config()
    base = str(cfg.get("base_url") or "https://c168b2.cc").rstrip("/")
    log_file = _DIR / "c168_login.jsonl"
    log_file.write_text("", encoding="utf-8")

    login_hits: list[dict[str, Any]] = []

    def on_request(req):
        if not HALL_API_RE.search(req.url):
            return
        _log_append(
            log_file,
            {
                "kind": "request",
                "method": req.method,
                "path": _api_path(req.url),
            },
        )

    def on_response(resp):
        if not HALL_API_RE.search(resp.url):
            return
        try:
            body = resp.text()[:12_000]
        except BaseException:
            return
        path = _api_path(resp.url)
        _log_append(
            log_file,
            {
                "kind": "response",
                "status": resp.status,
                "path": path,
                "body": body[:2000],
            },
        )
        low = path.lower()
        if "member/login" in low or "getfastlogin" in low or "member/sign" in low:
            login_hits.append({"path": path, "status": resp.status, "body": body})

    result: dict[str, Any] = {
        "ok": False,
        "username": username,
        "log_file": str(log_file),
        "proxy": proxy_log_label(proxy),
    }

    if not proxy.strip():
        result["error"] = "Cần proxy (đăng nhập phải qua proxy)"
        return result

    print(
        "Chuẩn bị Chrome C168 riêng (profile c168-chrome-profile, không tắt Chrome cá nhân)…",
        file=sys.stderr,
    )
    _wipe_c168_chrome_profile(kill_chrome=True)

    print(f"Mở Chrome debug — {result['proxy']}…", file=sys.stderr)
    ok, msg = _start_chrome_debug(base=base, reg_path="/home/login", proxy=proxy)
    if not ok:
        result["error"] = f"Không mở Chrome: {msg}"
        return result
    print(f"Chrome OK: {msg}", file=sys.stderr)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        result["error"] = "pip install playwright"
        return result

    with sync_playwright() as p:
        browser = None
        page = None
        try:
            browser = p.chromium.connect_over_cdp(CDP_DEFAULT_URL)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            _bind_manual_cdp_browser(browser, on_request, on_response)
            _apply_mobile_viewport(page)
            _open_login_dialog(page, base)
            filled = _fill_login_form(page, username, password)
            result["filled_fields"] = filled
            if not all(filled.values()):
                result["error"] = f"Không điền được login: {filled}"
                return result

            _dismiss_blocking_popups(page)
            pw_cfg = cfg.get("playwright") if isinstance(cfg.get("playwright"), dict) else {}
            _pause(page, int(pw_cfg.get("pause_before_submit_ms") or 2000))
            print("Bấm ĐĂNG NHẬP…", file=sys.stderr)
            _click_login_submit(page)

            deadline = time.time() + 45
            while time.time() < deadline:
                if login_hits:
                    try:
                        j = json.loads(str(login_hits[-1].get("body") or "{}"))
                        if j.get("code") == 1:
                            result["ok"] = True
                            break
                    except Exception:
                        pass
                if _detect_logged_in(page):
                    result["ok"] = True
                    result["logged_in_ui"] = True
                    break
                page.wait_for_timeout(500)

            page.wait_for_timeout(3000)
            if _detect_logged_in(page):
                result["ok"] = True

            if keep_browser_open:
                print(
                    "\nChrome C168 vẫn mở — đóng cửa sổ khi bạn xong.\n",
                    file=sys.stderr,
                )
        except Exception as e:
            result["error"] = str(e)
        finally:
            result["login_calls"] = [
                {
                    "path": h.get("path"),
                    "status": h.get("status"),
                    "body_preview": (h.get("body") or "")[:300],
                }
                for h in login_hits
            ]
            _log_append(
                log_file,
                {"kind": "summary", "ts": _ts(), "ok": result.get("ok")},
            )
            status = "OK" if result.get("ok") else "THẤT BẠI"
            print(f"\nĐăng nhập: {status} — user={username}\n", file=sys.stderr)
            _close_browser(
                browser,
                page,
                keep_open_ms=-1 if keep_browser_open else 0,
                headless=False,
                kill_chrome=not keep_browser_open,
            )

    return result


def _ask(label: str, *, secret: bool = False) -> str:
    while True:
        if secret:
            raw = getpass.getpass(f"{label}: ")
        else:
            raw = input(f"{label}: ")
        val = raw.strip()
        if val:
            return val
        print("  → Không được để trống.", file=sys.stderr)


def _prompt_login_fields(args: argparse.Namespace) -> argparse.Namespace:
    if args.username and args.password and args.proxy:
        return args
    print("\n══════════════════════════════════════", file=sys.stderr)
    print("  C168 — ĐĂNG NHẬP", file=sys.stderr)
    print("══════════════════════════════════════\n", file=sys.stderr)
    if not args.username:
        args.username = _ask("Tên tài khoản")
    if not args.password:
        args.password = _ask("Mật khẩu", secret=True)
    if not args.proxy:
        args.proxy = _ask("Proxy SOCKS5 (host:port:user:pass)")
    print("", file=sys.stderr)
    return args


def main() -> int:
    parser = argparse.ArgumentParser(
        description="C168: Đăng nhập qua proxy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-u", "--username", default="", help="Tên tài khoản")
    parser.add_argument("-p", "--password", default="", help="Mật khẩu")
    parser.add_argument(
        "--proxy",
        default="",
        help="SOCKS5 host:port:user:pass",
    )
    parser.add_argument(
        "--close-browser",
        action="store_true",
        help="Tự đóng Chrome C168 sau khi xong (mặc định: giữ mở)",
    )
    args = parser.parse_args()
    args = _prompt_login_fields(args)

    try:
        parse_proxy(args.proxy.strip())
    except ValueError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1

    out = login(
        username=args.username.strip(),
        password=args.password,
        proxy=args.proxy.strip(),
        cfg=load_config(),
        keep_browser_open=not args.close_browser,
    )

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
