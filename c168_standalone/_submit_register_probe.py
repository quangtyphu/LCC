# -*- coding: utf-8 -*-
import json
import random
import string
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = "https://c168b2.cc"
OUT = Path(__file__).resolve().parent / "_submit_probe.json"


def rnd_user() -> str:
    return "qc" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


def rnd_pass() -> str:
    return "Abc" + "".join(random.choices(string.digits, k=6))


def rnd_phone() -> str:
    # Site yêu cầu SĐT không có số 0 đầu (prefix +84 hiển thị riêng)
    return "9" + "".join(random.choices(string.digits, k=8))


events: list[dict] = []


def on_req(req):
    if "hall/api" in req.url and req.method in ("POST", "PUT"):
        events.append(
            {
                "t": "req",
                "method": req.method,
                "url": req.url,
                "mode": req.headers.get("x-data-mode"),
                "post": (req.post_data or "")[:800],
            }
        )


def on_resp(resp):
    if "hall/api" in resp.url and resp.request.method in ("POST", "PUT"):
        try:
            body = resp.text()[:1200]
        except Exception:
            body = ""
        events.append({"t": "resp", "status": resp.status, "url": resp.url, "body": body})


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 390, "height": 844})
    page.on("request", on_req)
    page.on("response", on_resp)
    page.goto(BASE + "/home/register", wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(5000)

    vals = [rnd_user(), rnd_pass(), rnd_phone(), "NGUYEN VAN A"]
    text_inputs = page.locator("input[type='text']:visible, input[type='tel']:visible")
    for i in range(min(text_inputs.count(), len(vals))):
        text_inputs.nth(i).fill(vals[i])

    try:
        page.locator("input[type='checkbox']").first.check(force=True, timeout=2000)
    except Exception:
        pass

    page.get_by_role("button", name="ĐĂNG KÝ").last.click(force=True, timeout=15_000)
    page.wait_for_timeout(15000)

    captcha = page.evaluate(
        """() => ({
          geetest: !!document.querySelector('.geetest_panel, [class*="geetest"]'),
          imgs: [...document.querySelectorAll('img')].slice(0,5).map(i => i.src),
          err: [...document.querySelectorAll('[class*="error"],[class*="toast"]')]
            .map(e => (e.innerText||'').trim()).filter(Boolean).slice(0,5),
        })"""
    )
    browser.close()

OUT.write_text(
    json.dumps(
        {"events": events, "captcha": captcha, "username": vals[0], "phone": vals[2]},
        indent=2,
        ensure_ascii=False,
    ),
    encoding="utf-8",
)
