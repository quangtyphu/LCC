# -*- coding: utf-8 -*-
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = "https://c168b2.cc"
LOG = Path(__file__).resolve().parent / "_ui_mobile.txt"
lines: list[str] = []


def log(s: str) -> None:
    lines.append(s)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        is_mobile=True,
        has_touch=True,
        user_agent=(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
        ),
        locale="vi-VN",
    )
    page = ctx.new_page()
    page.goto(BASE + "/#Register", wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(10000)
    log(f"url={page.url}")
    inputs = page.locator("input:visible")
    log(f"inputs={inputs.count()}")
    for i in range(min(inputs.count(), 20)):
        el = inputs.nth(i)
        log(f"{i} type={el.get_attribute('type')!r} ph={el.get_attribute('placeholder')!r}")
    page.screenshot(path="_ui_mobile_register.png", full_page=True)
    browser.close()

LOG.write_text("\n".join(lines), encoding="utf-8")
