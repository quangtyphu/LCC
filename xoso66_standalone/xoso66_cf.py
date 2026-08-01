# -*- coding: utf-8 -*-
"""
Tự lấy header cf-* (+ cf_clearance nếu có) — không copy tay từ DevTools.

Thứ tự:
  1. curl_cffi (Chrome TLS) → cookie cf_clearance (legacy)
  2. Playwright headless → bắt header cf-* từ XHR /server/*

ok = có cf-auth-token (cf_clearance không bắt buộc — site thường không còn set).

Cài thêm:
  pip install curl_cffi playwright
  playwright install chromium
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any
from urllib.parse import urlparse

BASE_URL = os.environ.get("XOSO66_BASE_URL", "https://v6sgqpyi.whskxk1.com").rstrip("/")
SITE_HOST = urlparse(BASE_URL).netloc

CF_COOKIES = ("cf_clearance", "__cf_bm")
CF_HEADERS = ("cf-auth-token", "cf-con-s", "cf-pass", "c-a-i")
CF_VERIFY_FRAGMENT = "/__verify/check"


def session_cf_ready(session: dict) -> bool:
    """
    Đủ CF để login / gọi API site.
    Hiện tại chỉ cần cf-auth-token; cf_clearance là optional (thường không còn).
    """
    return bool((session.get("headers") or {}).get("cf-auth-token"))

_CF_RATE_LIMIT_UNTIL: dict[str, float] = {}
_CF_RATE_LIMIT_LOCK = threading.Lock()
# Cooldown bot tự đặt sau 1015 — mặc định 2 phút (không gọi API/WS trong lúc chờ).
DEFAULT_CF_COOLDOWN_SEC = int(os.environ.get("XOSO66_CF_RATE_LIMIT_COOLDOWN_SEC", "120"))


class CfRateLimitError(RuntimeError):
    """Proxy IP đang bị Cloudflare rate limit (1015)."""

    def __init__(self, message: str = "", *, remaining_sec: float = 0) -> None:
        super().__init__(message)
        self.remaining_sec = remaining_sec


def proxy_cooldown_key(session: dict | str) -> str:
    from xoso66_cms_chrome import _proxy_key
    from xoso66_proxy import resolve_proxy

    if isinstance(session, str):
        px = session.strip()
        return _proxy_key(px) if px else ""
    px = resolve_proxy(session)
    return _proxy_key(px) if px else ""


def is_cloudflare_rate_limited(text: str = "", status: int | None = None) -> bool:
    t = (text or "").lower()
    if "error 1015" in t or "you are being rate limited" in t:
        return True
    if "banned you temporarily" in t and "cloudflare" in t:
        return True
    if status == 429:
        return True
    if status in (403, 503) and ("1015" in t or "rate limit" in t):
        return True
    return False


def mark_cf_rate_limited(session: dict | str, *, cooldown_sec: int | None = None) -> float:
    key = proxy_cooldown_key(session)
    if not key:
        return 0.0
    sec = max(30, int(cooldown_sec or DEFAULT_CF_COOLDOWN_SEC))
    until = time.time() + sec
    with _CF_RATE_LIMIT_LOCK:
        _CF_RATE_LIMIT_UNTIL[key] = until
    return until


def cf_rate_limit_remaining(session: dict | str) -> float:
    key = proxy_cooldown_key(session)
    if not key:
        return 0.0
    with _CF_RATE_LIMIT_LOCK:
        until = _CF_RATE_LIMIT_UNTIL.get(key, 0.0)
    return max(0.0, until - time.time())


def is_cf_rate_limited(session: dict | str) -> bool:
    return cf_rate_limit_remaining(session) > 0


def cf_rate_limit_wait_label(remaining_sec: float) -> str:
    rem = max(0.0, float(remaining_sec))
    if rem < 90:
        return f"~{max(1, int(rem))}s"
    mins = max(1, int((rem + 59) // 60))
    return f"~{mins} phút"


def cf_rate_limit_message(session: dict | str) -> str:
    rem = cf_rate_limit_remaining(session)
    return (
        f"IP proxy đang bị Cloudflare rate limit (1015). "
        f"Chờ {cf_rate_limit_wait_label(rem)} hoặc đổi proxy SOCKS5 trên CMS."
    )


def cf_rate_limit_remaining_for_account(account_id: str) -> float:
    """Cooldown CF theo proxy của account (DB row hoặc session)."""
    aid = str(account_id or "").strip()
    if not aid:
        return 0.0
    from xoso66_accounts_db import get_account
    from xoso66_proxy import resolve_proxy

    row = get_account(aid) or {}
    px = resolve_proxy(row)
    if px:
        return cf_rate_limit_remaining(px)
    try:
        from xoso66_session import load_sessions

        sess = (load_sessions() or {}).get(aid) or {}
        px2 = resolve_proxy(sess)
        if px2:
            return cf_rate_limit_remaining(px2)
        return cf_rate_limit_remaining(sess)
    except Exception:
        return 0.0


def is_account_cf_rate_limited(account_id: str) -> bool:
    return cf_rate_limit_remaining_for_account(account_id) > 0


def row_cf_rate_limited(row: dict[str, Any]) -> bool:
    """True nếu proxy của row đang trong cooldown CF."""
    from xoso66_proxy import resolve_proxy

    px = resolve_proxy(row or {})
    if px:
        return is_cf_rate_limited(px)
    return is_cf_rate_limited(row or {})


def _proxy_dict(session: dict) -> dict | None:
    from xoso66_proxy import build_proxies, ensure_proxy

    ensure_proxy(session)
    return build_proxies(session["proxy"])


def _apply_cookies(session: dict, cookie_jar: Any) -> None:
    """Gộp cookie từ CF refresh — không lấy PHPSESSID (tránh lẫn phiên user)."""
    from xoso66_session import merge_session_cookies

    incoming: dict[str, Any] = {}
    if hasattr(cookie_jar, "items"):
        for name, value in cookie_jar.items():
            incoming[str(name)] = value
    elif isinstance(cookie_jar, list):
        for c in cookie_jar:
            if isinstance(c, dict) and c.get("name"):
                incoming[str(c["name"])] = str(c.get("value", ""))
    merge_session_cookies(session, incoming, allow_identity=False)


def refresh_cf_curl_cffi(session: dict) -> dict[str, Any]:
    """Thử lấy cf_clearance bằng TLS giống Chrome (không có cf-auth-token)."""
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        return {"ok": False, "method": "curl_cffi", "error": "chưa cài curl_cffi"}

    from xoso66_deposit import DEFAULT_UA

    ua = session.get("user_agent") or DEFAULT_UA
    hdrs = {
        "user-agent": ua,
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
    }
    try:
        r = cffi_requests.get(
            f"{BASE_URL}/home/",
            impersonate=os.environ.get("XOSO66_CF_IMPERSONATE", "chrome120"),
            headers=hdrs,
            cookies=session.get("cookies") or {},
            proxies=_proxy_dict(session),
            timeout=60,
            allow_redirects=True,
        )
    except Exception as e:
        return {"ok": False, "method": "curl_cffi", "error": str(e)}

    if is_cloudflare_rate_limited(r.text or "", r.status_code):
        mark_cf_rate_limited(session)
        return {
            "ok": False,
            "method": "curl_cffi",
            "rate_limited": True,
            "http_status": r.status_code,
            "error": cf_rate_limit_message(session),
        }

    _apply_cookies(session, r.cookies)
    has_clearance = bool((session.get("cookies") or {}).get("cf_clearance"))
    # curl_cffi không bắt cf-auth-token — chỉ clearance (legacy). Playwright mới đủ.
    return {
        "ok": has_clearance and session_cf_ready(session),
        "method": "curl_cffi",
        "http_status": r.status_code,
        "has_clearance": has_clearance,
        "has_cf_headers": session_cf_ready(session),
    }


def refresh_cf_playwright(session: dict, *, headless: bool | None = None) -> dict[str, Any]:
    """Mở trang chủ, vượt CF, bắt cookie + header từ request API."""
    try:
        from xoso66_playwright_ctx import playwright_browser
    except ImportError:
        return {"ok": False, "method": "playwright", "error": "pip install playwright && playwright install chromium"}

    from xoso66_deposit import DEFAULT_UA

    if headless is None:
        headless = os.environ.get("XOSO66_CF_HEADLESS", "1") != "0"

    captured_headers: dict[str, str] = {}
    ua = session.get("user_agent") or DEFAULT_UA

    from xoso66_playwright_ctx import playwright_browser

    try:
        session["user_agent"] = ua
        with playwright_browser(session, base_url=BASE_URL, headless=headless) as (
            _p,
            _browser,
            context,
        ):
            page = context.new_page()

            def on_request(request) -> None:
                if SITE_HOST not in request.url or "/server/" not in request.url:
                    return
                h = request.headers
                for key in CF_HEADERS:
                    val = h.get(key)
                    if val:
                        captured_headers[key] = val
                ft = h.get("form-token")
                if ft:
                    session["form_token"] = ft

            page.on("request", on_request)
            page.goto(f"{BASE_URL}/home/", wait_until="domcontentloaded", timeout=90_000)
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                page.wait_for_timeout(8_000)

            try:
                page_html = page.content()
            except Exception:
                page_html = ""
            if is_cloudflare_rate_limited(page_html):
                mark_cf_rate_limited(session)
                return {
                    "ok": False,
                    "method": "playwright",
                    "rate_limited": True,
                    "error": cf_rate_limit_message(session),
                }

            _apply_cookies(session, context.cookies())
    except Exception as e:
        err = str(e).strip() or f"{type(e).__name__}: {e!r}"
        return {"ok": False, "method": "playwright", "error": err}

    hdrs = dict(session.get("headers") or {})
    for key in CF_HEADERS:
        if captured_headers.get(key):
            hdrs[key] = captured_headers[key]
    session["headers"] = hdrs

    ok = session_cf_ready(session)
    return {
        "ok": ok,
        "method": "playwright",
        "has_clearance": bool((session.get("cookies") or {}).get("cf_clearance")),
        "has_cf_headers": ok,
        "captured_headers": list(captured_headers.keys()),
    }


def refresh_cloudflare(session: dict, *, prefer_playwright: bool = False) -> dict[str, Any]:
    """
    Tự renew Cloudflare. Trả report {ok, method, ...}.
    ok = có cf-auth-token (cf_clearance không bắt buộc).
    """
    from xoso66_proxy import proxy_has_auth, resolve_proxy

    if is_cf_rate_limited(session):
        return {
            "ok": False,
            "rate_limited": True,
            "remaining_sec": cf_rate_limit_remaining(session),
            "error": cf_rate_limit_message(session),
            "steps": [],
        }

    steps: list[dict] = []
    px = resolve_proxy(session)
    # SOCKS5 có auth: curl_cffi thường không đủ → Playwright qua relay local
    if prefer_playwright or proxy_has_auth(px):
        r2 = refresh_cf_playwright(session)
        steps.append(r2)
        if session_cf_ready(session):
            return {"ok": True, "steps": steps}
        if proxy_has_auth(px):
            return {"ok": False, "steps": steps}

    r1 = refresh_cf_curl_cffi(session)
    steps.append(r1)
    if session_cf_ready(session):
        return {"ok": True, "steps": steps}

    if not any(s.get("method") == "playwright" for s in steps):
        r2 = refresh_cf_playwright(session)
        steps.append(r2)
    ok = session_cf_ready(session)
    rate_limited = any(s.get("rate_limited") for s in steps)
    return {"ok": ok, "steps": steps, "rate_limited": rate_limited}


def is_cf_verify_url(url: str) -> bool:
    return CF_VERIFY_FRAGMENT in str(url or "")


def capsolver_proxy_url(proxy_str: str) -> str:
    from xoso66_proxy import parse_proxy

    host, port, user, pwd = parse_proxy(proxy_str)
    if user:
        return f"socks5://{user}:{pwd}@{host}:{port}"
    return f"socks5://{host}:{port}"


def attach_cf_request_sniffer(page: Any, session: dict) -> None:
    """Bắt cf-auth-token + form-token từ XHR /server/* trên cùng tab đăng ký."""

    def on_request(request) -> None:
        if SITE_HOST not in request.url or "/server/" not in request.url:
            return
        h = request.headers
        hdrs = dict(session.get("headers") or {})
        for key in CF_HEADERS:
            val = h.get(key)
            if val:
                hdrs[key] = val
        session["headers"] = hdrs
        ft = h.get("form-token")
        if ft:
            session["form_token"] = ft

    page.on("request", on_request)


def _sync_session_cookies_to_context(context: Any, session: dict, host: str) -> None:
    cookies = session.get("cookies") or {}
    if not cookies:
        return
    pw_cookies = []
    for name, value in cookies.items():
        if value is None:
            continue
        pw_cookies.append(
            {
                "name": str(name),
                "value": str(value),
                "domain": host,
                "path": "/",
            }
        )
    if pw_cookies:
        try:
            context.add_cookies(pw_cookies)
        except Exception:
            pass


def wait_past_cf_verify(page: Any, *, timeout_ms: int = 45_000) -> dict[str, Any]:
    """Chờ rời trang CF /__verify/check (tự pass, giải tay, hoặc Capsolver)."""
    import time

    from xoso66_captcha_solver import PAGE_LOAD_DIAG_JS

    if is_cf_verify_url(page.url) and timeout_ms >= 60_000:
        print(
            "[REGISTER] Đang ở trang chọn con vật (Cloudflare). "
            "Nếu Chrome hiện captcha — bấm chọn ảnh + Xác nhận trong cửa sổ Chrome.",
            flush=True,
        )

    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        if not is_cf_verify_url(page.url):
            return {"ok": True, "url": page.url}
        page.wait_for_timeout(1_500)
    diag: dict[str, Any] = {}
    try:
        raw = page.evaluate(PAGE_LOAD_DIAG_JS)
        if isinstance(raw, dict):
            diag = raw
    except Exception:
        pass
    return {
        "ok": False,
        "error": "cf_verify_timeout",
        "url": page.url,
        "diag": diag,
    }


def bootstrap_register_page(
    page: Any,
    session: dict,
    *,
    context: Any = None,
    headless: bool = True,
    native_launch: bool = False,
) -> dict[str, Any]:
    """
    Mở /home/, vượt CF verify, chờ Vue mount.
    native_launch=True: Chrome đã mở /home/ — không goto lại (tránh CF re-challenge).
    """
    from xoso66_captcha_solver import (
        PAGE_LOAD_DIAG_JS,
        solve_cf_anticloudflare,
        vue_store_unavailable_message,
        wait_for_vue_store,
    )
    from xoso66_proxy import site_host as _site_host

    host = _site_host(BASE_URL)
    meta: dict[str, Any] = {}
    cookies = session.get("cookies") or {}
    if not native_launch or not cookies.get("cf_clearance"):
        meta["curl_cf"] = refresh_cf_curl_cffi(session)
    attach_cf_request_sniffer(page, session)

    vue_wait: dict[str, Any] = {"ok": False}
    for attempt in range(3):
        cur = str(page.url or "")
        on_site = SITE_HOST in cur and "about:blank" not in cur
        if native_launch and attempt == 0 and on_site:
            meta["skip_goto"] = True
        else:
            page.goto(f"{BASE_URL}/home/", wait_until="domcontentloaded", timeout=90_000)
        try:
            page.wait_for_load_state("networkidle", timeout=25_000)
        except Exception:
            page.wait_for_timeout(8_000)
        page.wait_for_timeout(2_000)

        if is_cf_verify_url(page.url):
            meta["cf_verify"] = True
            print(
                f"[REGISTER] Cloudflare verify ({page.url})",
                flush=True,
            )
            manual_ms = int(os.environ.get("XOSO66_CF_MANUAL_WAIT_SEC", "180")) * 1000
            brief_wait = manual_ms if not headless else min(manual_ms, 15_000)
            passed = wait_past_cf_verify(page, timeout_ms=brief_wait)
            meta[f"cf_wait_{attempt}"] = passed
            if not passed.get("ok"):
                page_html = ""
                try:
                    page_html = page.content()
                except Exception:
                    pass
                solved = solve_cf_anticloudflare(
                    session,
                    website_url=page.url or f"{BASE_URL}/home/",
                    html=page_html,
                )
                meta[f"cf_capsolver_{attempt}"] = {
                    "ok": solved.get("ok"),
                    "error": solved.get("error"),
                }
                if solved.get("ok") and context is not None:
                    _sync_session_cookies_to_context(context, session, host)
                    page.reload(wait_until="domcontentloaded", timeout=90_000)
                    page.wait_for_timeout(3_000)
                    continue
                if attempt + 1 >= 3:
                    diag = passed.get("diag") or {}
                    try:
                        raw = page.evaluate(PAGE_LOAD_DIAG_JS)
                        if isinstance(raw, dict):
                            diag = raw
                    except Exception:
                        pass
                    return {
                        "ok": False,
                        "error": "cf_verify_blocked",
                        "msg": (
                            "Cloudflare chặn /__verify/check (chọn con vật). "
                            "Giải captcha trong cửa sổ Chrome (tối đa 3 phút) hoặc đặt "
                            "XOSO66_REGISTER_PROFILE_DIR trỏ profile Chrome đang dùng tay."
                        ),
                        "diag": diag,
                        "meta": meta,
                    }
                continue

        vue_wait = wait_for_vue_store(page, timeout_ms=60_000 if attempt else 90_000)
        if vue_wait.get("ok"):
            meta["vue_ok"] = True
            return {"ok": True, "vue_wait": vue_wait, "meta": meta}

        if attempt < 2:
            page.reload(wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(3_000)

    return {
        "ok": False,
        "error": "no_vue_store",
        "msg": vue_store_unavailable_message(vue_wait),
        "vue_wait": vue_wait,
        "meta": meta,
    }


def _inject_session_cookies(context: Any, session: dict, host: str) -> None:
    cookies = session.get("cookies") or {}
    if not cookies:
        return
    parts = str(host or SITE_HOST).split(".")
    domains = [str(host or SITE_HOST)]
    if len(parts) >= 2:
        domains.append("." + ".".join(parts[-2:]))
    pw_cookies: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for domain in domains:
        for name, value in cookies.items():
            if value is None:
                continue
            key = (str(name), domain)
            if key in seen:
                continue
            seen.add(key)
            pw_cookies.append(
                {
                    "name": str(name),
                    "value": str(value),
                    "domain": domain,
                    "path": "/",
                }
            )
    if pw_cookies:
        try:
            context.add_cookies(pw_cookies)
        except Exception:
            pass


def sniff_cf_request_headers(session: dict, *, headless: bool | None = None) -> dict[str, Any]:
    """
    Bắt cf-auth-token từ XHR /server/* khi đã có cf_clearance.

    Dùng browser tạm (không profile CMS đang lock) + nạp cookie từ session.
    """
    hdrs = dict(session.get("headers") or {})
    if hdrs.get("cf-auth-token"):
        return {"ok": True, "skipped": True, "has_cf_headers": True}

    if not (session.get("cookies") or {}).get("cf_clearance"):
        return {"ok": False, "error": "missing_cf_clearance", "method": "sniff_headers"}

    if headless is None:
        headless = os.environ.get("XOSO66_CF_HEADLESS", "0") != "0"

    from xoso66_playwright_ctx import playwright_browser

    host = SITE_HOST
    try:
        with playwright_browser(
            session,
            base_url=BASE_URL,
            headless=headless,
            channel="chrome",
            ignore_automation=True,
        ) as (_p, _browser, context):
            _inject_session_cookies(context, session, host)
            page = context.new_page()
            attach_cf_request_sniffer(page, session)
            print("[REGISTER] Sniff cf-auth-token (Chrome tạm + cookie CMS)…", flush=True)
            page.goto(f"{BASE_URL}/home/", wait_until="domcontentloaded", timeout=90_000)
            try:
                page.wait_for_load_state("networkidle", timeout=25_000)
            except Exception:
                page.wait_for_timeout(8_000)
            page.wait_for_timeout(3_000)
            _apply_cookies(session, context.cookies())
    except Exception as e:
        err = str(e).strip() or f"{type(e).__name__}: {e!r}"
        return {"ok": False, "method": "sniff_headers", "error": err}

    ok = bool((session.get("headers") or {}).get("cf-auth-token"))
    return {
        "ok": ok,
        "method": "sniff_headers",
        "has_cf_headers": ok,
        "captured": sorted((session.get("headers") or {}).keys()),
    }
