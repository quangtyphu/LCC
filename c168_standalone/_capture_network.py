# -*- coding: utf-8 -*-
"""Capture register-related XHR from C168 (one-shot debug)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = "https://c168b2.cc"
OUT = Path(__file__).resolve().parent / "_captured_requests.jsonl"


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("pip install playwright && playwright install chromium", file=sys.stderr)
        return 1

    hits: list[dict] = []

    def on_request(req):
        url = req.url
        if any(
            x in url.lower()
            for x in (
                "register",
                "captcha",
                "geetest",
                "sms",
                "member",
                "wps",
                "api",
                "user",
                "auth",
                "2865",
            )
        ):
            hits.append(
                {
                    "method": req.method,
                    "url": url,
                    "headers": dict(req.headers),
                    "post": req.post_data[:2000] if req.post_data else None,
                }
            )

    def on_response(resp):
        url = resp.url
        if any(
            x in url.lower()
            for x in ("register", "captcha", "geetest", "sms", "member", "wps", "api")
        ):
            try:
                body = resp.text()[:3000]
            except Exception:
                body = ""
            hits.append(
                {
                    "type": "response",
                    "status": resp.status,
                    "url": url,
                    "body": body,
                }
            )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="vi-VN",
        )
        page = ctx.new_page()
        page.on("request", on_request)
        page.on("response", on_response)
        page.goto(BASE + "/", wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(8000)
        # try hash register
        page.goto(BASE + "/#Register", wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(10000)
        browser.close()

    with OUT.open("w", encoding="utf-8") as f:
        for h in hits:
            f.write(json.dumps(h, ensure_ascii=False) + "\n")
    print(f"captured {len(hits)} -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
