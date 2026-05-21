# -*- coding: utf-8 -*-
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = "https://c168b2.cc"
OUT = Path(__file__).resolve().parent / "_vue_probe.json"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 390, "height": 844})
    page.goto(BASE + "/", wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(8000)
    data = page.evaluate(
        """() => {
          const app = document.querySelector('#app');
          const keys = app ? Object.keys(app) : [];
          const vue = app && (app.__vue_app__ || app._vnode?.appContext);
          let storeKeys = [];
          try {
            const pinia = window.__PINIA__;
            if (pinia) storeKeys = Object.keys(pinia._s || {});
          } catch (e) {}
          let routes = [];
          try {
            const r = vue?.config?.globalProperties?.$router?.getRoutes?.();
            if (r) routes = r.map(x => x.path).slice(0, 40);
          } catch (e) {}
          return { appKeys: keys, storeKeys, routes, hash: location.hash };
        }"""
    )
    page.goto(BASE + "/#Register", wait_until="domcontentloaded")
    page.wait_for_timeout(6000)
    data2 = page.evaluate(
        """() => ({
          hash: location.hash,
          dialogs: [...document.querySelectorAll('[class*="loginRegister"],[class*="LoginRegister"]')].map(
            el => el.className
          ),
          overlays: document.querySelectorAll('.ui-overlay').length,
        })"""
    )
    browser.close()

OUT.write_text(json.dumps({"boot": data, "register": data2}, indent=2), encoding="utf-8")
