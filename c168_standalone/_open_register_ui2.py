# -*- coding: utf-8 -*-
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = "https://c168b2.cc"
LOG = Path(__file__).resolve().parent / "_ui_debug2.txt"
lines: list[str] = []


def log(s: str) -> None:
    lines.append(s)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.goto(BASE + "/#Register", wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(8000)

    # dismiss overlays
    for sel in [
        ".ui-dialog__close",
        "[class*='close']",
        "button:visible",
    ]:
        try:
            page.locator(sel).first.click(timeout=1500, force=True)
            page.wait_for_timeout(500)
        except Exception:
            pass

    dlg = page.locator("[class*='loginRegisterDialog'], .loginRegisterFragment")
    log(f"dialog count={dlg.count()}")
    inputs = page.locator(
        "[class*='loginRegister'] input:visible, .loginRegisterFragment input:visible, "
        ".ui-input input:visible, input:visible"
    )
    log(f"inputs={inputs.count()}")
    for i in range(min(inputs.count(), 20)):
        el = inputs.nth(i)
        log(
            f"{i} type={el.get_attribute('type')!r} "
            f"ph={el.get_attribute('placeholder')!r}"
        )
    page.screenshot(path="_ui_register.png", full_page=True)
    browser.close()

LOG.write_text("\n".join(lines), encoding="utf-8")
