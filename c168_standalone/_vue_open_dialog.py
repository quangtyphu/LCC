# -*- coding: utf-8 -*-
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = "https://c168b2.cc"
OUT = Path(__file__).resolve().parent / "_vue_open.json"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 390, "height": 844})
    page.goto(BASE + "/home/mine", wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(6000)
    res = page.evaluate(
        """async () => {
          const app = document.querySelector('#app').__vue_app__;
          const router = app.config.globalProperties.$router;
          const routes = router.getRoutes().map(r => r.path).filter(
            p => /login|register|auth|sign/i.test(p)
          );
          const store = app.config.globalProperties.$pinia || window.__PINIA__;
          let piniaIds = [];
          try {
            piniaIds = store ? Object.keys(store._s) : [];
          } catch (e) {}
          // try common dialog opens
          const tries = [];
          const gp = app.config.globalProperties;
          for (const name of [
            'openLoginRegister', 'showLoginRegister', 'openRegister',
            'showRegister', 'openLogin', 'showLogin',
          ]) {
            if (typeof gp[name] === 'function') {
              try { gp[name](1); tries.push('gp.' + name); } catch (e) {}
            }
          }
          try { await router.push('/#Register'); tries.push('router push hash'); } catch (e) {}
          try { location.hash = '#Register'; tries.push('hash'); } catch (e) {}
          // click un-login
          const el = document.querySelector('[class*=\"un-login\"]');
          if (el) { el.click(); tries.push('click un-login'); }
          await new Promise(r => setTimeout(r, 3000));
          return {
            routes,
            piniaIds,
            tries,
            inputs: document.querySelectorAll('input').length,
            visibleInputs: [...document.querySelectorAll('input')].filter(
              i => i.offsetParent !== null
            ).length,
            dialogClasses: [...document.querySelectorAll('[class*=\"loginRegister\"]')].map(
              e => e.className
            ),
          };
        }"""
    )
    browser.close()

OUT.write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
