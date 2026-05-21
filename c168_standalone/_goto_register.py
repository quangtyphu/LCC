# -*- coding: utf-8 -*-
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = "https://c168b2.cc"
OUT = Path(__file__).resolve().parent / "_goto_register.json"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 390, "height": 844})
    page.goto(BASE + "/home/register", wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(8000)
    posts: list[dict] = []

    def on_req(req):
        if req.method == "POST" and "hall/api" in req.url:
            posts.append({"url": req.url, "post": (req.post_data or "")[:500]})

    page.on("request", on_req)
    info = page.evaluate(
        """() => ({
          url: location.href,
          inputs: [...document.querySelectorAll('input')].map(i => ({
            type: i.type, ph: i.placeholder, name: i.name, visible: i.offsetParent !== null
          })),
          buttons: [...document.querySelectorAll('button')].slice(0,15).map(
            b => (b.innerText || '').trim().slice(0,40)
          ),
        })"""
    )
    page.wait_for_timeout(3000)
    browser.close()

OUT.write_text(json.dumps({"info": info, "posts": posts}, indent=2, ensure_ascii=False), encoding="utf-8")
