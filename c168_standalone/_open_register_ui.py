# -*- coding: utf-8 -*-
"""Debug: open register UI and dump visible controls."""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "https://c168b2.cc"
LOG = Path(__file__).resolve().parent / "_ui_debug.txt"
lines: list[str] = []


def log(msg: str) -> None:
    lines.append(msg)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.goto(BASE, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(5000)
    for sel in [
        "text=Đăng nhập",
        "text=Đăng Nhập",
        "text=Login",
        "text=Đăng ký",
        "[class*='login']",
        "[class*='Login']",
    ]:
        loc = page.locator(sel)
        log(f"{sel!r} count={loc.count()}")
        if loc.count():
            try:
                loc.first.click(timeout=3000)
                page.wait_for_timeout(3000)
            except Exception as e:
                log(f"  click err {e}")
    page.screenshot(path="_ui_after_click.png", full_page=True)
    inputs = page.locator("input:visible")
    log(f"inputs={inputs.count()}")
    for i in range(min(inputs.count(), 15)):
        el = inputs.nth(i)
        log(
            f"{i} type={el.get_attribute('type')!r} "
            f"ph={el.get_attribute('placeholder')!r} name={el.get_attribute('name')!r}"
        )
    browser.close()
LOG.write_text("\n".join(lines), encoding="utf-8")
sys.exit(0)
