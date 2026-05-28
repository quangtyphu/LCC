# -*- coding: utf-8 -*-
"""
Tự lấy cf_clearance + header cf-* (Cloudflare) — không copy tay từ DevTools.

Thứ tự:
  1. curl_cffi (Chrome TLS) → cookie cf_clearance
  2. Playwright headless → cookie + bắt header từ XHR /server/*

Cài thêm:
  pip install curl_cffi playwright
  playwright install chromium
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

BASE_URL = os.environ.get("XOSO66_BASE_URL", "https://v6sgqpyi.whskxk1.com").rstrip("/")
SITE_HOST = urlparse(BASE_URL).netloc

CF_COOKIES = ("cf_clearance", "__cf_bm")
CF_HEADERS = ("cf-auth-token", "cf-con-s", "cf-pass", "c-a-i")


def _proxy_dict(session: dict) -> dict | None:
    from xoso66_proxy import build_proxies, ensure_proxy

    ensure_proxy(session)
    return build_proxies(session["proxy"])


def _apply_cookies(session: dict, cookie_jar: Any) -> None:
    cookies = dict(session.get("cookies") or {})
    if hasattr(cookie_jar, "items"):
        for name, value in cookie_jar.items():
            cookies[name] = value
    elif isinstance(cookie_jar, list):
        for c in cookie_jar:
            if isinstance(c, dict) and c.get("name"):
                cookies[str(c["name"])] = str(c.get("value", ""))
    session["cookies"] = cookies


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
            impersonate=os.environ.get("XOSO66_CF_IMPERSONATE", "chrome131"),
            headers=hdrs,
            cookies=session.get("cookies") or {},
            proxies=_proxy_dict(session),
            timeout=60,
            allow_redirects=True,
        )
    except Exception as e:
        return {"ok": False, "method": "curl_cffi", "error": str(e)}

    _apply_cookies(session, r.cookies)
    ok = bool((session.get("cookies") or {}).get("cf_clearance"))
    return {
        "ok": ok,
        "method": "curl_cffi",
        "http_status": r.status_code,
        "has_clearance": ok,
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

            _apply_cookies(session, context.cookies())
    except Exception as e:
        err = str(e).strip() or f"{type(e).__name__}: {e!r}"
        return {"ok": False, "method": "playwright", "error": err}

    hdrs = dict(session.get("headers") or {})
    for key in CF_HEADERS:
        if captured_headers.get(key):
            hdrs[key] = captured_headers[key]
    session["headers"] = hdrs

    ok = bool((session.get("cookies") or {}).get("cf_clearance")) and bool(
        hdrs.get("cf-auth-token")
    )
    return {
        "ok": ok,
        "method": "playwright",
        "has_clearance": bool((session.get("cookies") or {}).get("cf_clearance")),
        "has_cf_headers": bool(hdrs.get("cf-auth-token")),
        "captured_headers": list(captured_headers.keys()),
    }


def refresh_cloudflare(session: dict, *, prefer_playwright: bool = False) -> dict[str, Any]:
    """
    Tự renew Cloudflare. Trả report {ok, method, ...}.
    ok = có cf_clearance; nên có thêm cf-auth-token (playwright).
    """
    from xoso66_proxy import proxy_has_auth, resolve_proxy

    steps: list[dict] = []
    px = resolve_proxy(session)
    # SOCKS5 có auth: curl_cffi thường không đủ → Playwright qua relay local
    if prefer_playwright or proxy_has_auth(px):
        r2 = refresh_cf_playwright(session)
        steps.append(r2)
        ok = bool((session.get("cookies") or {}).get("cf_clearance"))
        if ok:
            return {"ok": True, "steps": steps}
        if proxy_has_auth(px):
            return {"ok": False, "steps": steps}

    r1 = refresh_cf_curl_cffi(session)
    steps.append(r1)
    hdrs = session.get("headers") or {}
    if r1.get("ok") and hdrs.get("cf-auth-token"):
        return {"ok": True, "steps": steps}

    if not any(s.get("method") == "playwright" for s in steps):
        r2 = refresh_cf_playwright(session)
        steps.append(r2)
    ok = bool((session.get("cookies") or {}).get("cf_clearance"))
    return {"ok": ok, "steps": steps}
