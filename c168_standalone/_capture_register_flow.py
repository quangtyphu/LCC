# -*- coding: utf-8 -*-
"""Open register dialog, fill form, capture POST APIs."""
from __future__ import annotations

import json
import random
import string
import sys
from pathlib import Path

BASE = "https://c168b2.cc"
OUT = Path(__file__).resolve().parent / "_register_flow.jsonl"


def _rnd_user() -> str:
    return "qc" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


def _rnd_pass() -> str:
    return "Abc" + "".join(random.choices(string.digits, k=6))


def _rnd_phone() -> str:
    return "09" + "".join(random.choices(string.digits, k=8))


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("pip install playwright", file=sys.stderr)
        return 1

    hits: list[dict] = []

    def log(obj: dict) -> None:
        hits.append(obj)

    def on_request(req):
        if req.method in ("POST", "PUT") and "hall/api" in req.url:
            log(
                {
                    "method": req.method,
                    "url": req.url,
                    "headers": dict(req.headers),
                    "post": (req.post_data or "")[:4000],
                }
            )

    def on_response(resp):
        if "hall/api" in resp.url and resp.request.method in ("POST", "PUT"):
            try:
                body = resp.text()[:4000]
            except Exception:
                body = ""
            log({"type": "response", "status": resp.status, "url": resp.url, "body": body})

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="vi-VN",
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        page.on("request", on_request)
        page.on("response", on_response)
        page.goto(BASE + "/#Register", wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(6000)

        # click register tab if needed
        for sel in [
            "text=Đăng ký",
            "text=Đăng Ký",
            "text=Register",
            "[class*='register']",
        ]:
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=3000)
                    page.wait_for_timeout(1500)
                    break
            except Exception:
                pass

        inputs = page.locator("input:visible")
        n = inputs.count()
        log({"inputs_visible": n})
        values = [_rnd_user(), _rnd_pass(), _rnd_pass(), _rnd_phone(), "NGUYEN VAN A"]
        vi = 0
        for i in range(min(n, 12)):
            inp = inputs.nth(i)
            try:
                typ = (inp.get_attribute("type") or "").lower()
                ph = (inp.get_attribute("placeholder") or "").lower()
                name = (inp.get_attribute("name") or "").lower()
                if typ in ("hidden", "checkbox", "radio"):
                    continue
                val = ""
                if "pass" in ph or "pass" in name or typ == "password":
                    val = _rnd_pass()
                elif "phone" in ph or "mobile" in ph or "điện" in ph or "số" in ph:
                    val = _rnd_phone()
                elif "mail" in ph or typ == "email":
                    val = ""
                elif vi < len(values):
                    val = values[vi]
                    vi += 1
                if val:
                    inp.fill(val, timeout=2000)
            except Exception as e:
                log({"fill_err": str(e), "index": i})

        page.wait_for_timeout(2000)
        for sel in [
            "button:has-text('Đăng ký')",
            "button:has-text('Đăng Ký')",
            "button:has-text('Register')",
            ".ui-button--primary",
        ]:
            try:
                btn = page.locator(sel).first
                if btn.count() and btn.is_visible():
                    btn.click(timeout=5000)
                    page.wait_for_timeout(8000)
                    break
            except Exception:
                pass

        page.wait_for_timeout(5000)
        browser.close()

    with OUT.open("w", encoding="utf-8") as f:
        for h in hits:
            f.write(json.dumps(h, ensure_ascii=False) + "\n")
    print(f"wrote {len(hits)} events -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
