# -*- coding: utf-8 -*-
import re
import sys
from pathlib import Path

import requests

_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_DIR))

from xoso66_accounts_db import init_db
from xoso66_deposit import DEFAULT_UA, create_deposit_order
from xoso66_session import ensure_session

init_db()
rep = create_deposit_order("acc20", 100_000, session=ensure_session("acc20", force_login=False))
pay_url = rep["pay_url"]
headers = {"User-Agent": DEFAULT_UA, "referer": pay_url}
html = requests.get(pay_url, headers=headers, timeout=25).text
Path("_paymain_sample.html").write_text(html, encoding="utf-8")
print("paymain len", len(html))
for pat in [r'<img[^>]+src=["\']([^"\']+)["\']', r'qr[^"\']*', r'canvas', r'000201', r'qrcode', r'napas']:
    if pat.startswith("<"):
        found = re.findall(pat, html, re.I)
        print(pat, "->", len(found))
        for x in found[:15]:
            print(" ", x[:100])
    else:
        print(pat, "in html", pat.lower() in html.lower())
