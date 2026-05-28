#!/usr/bin/env python3
import re
import requests

t = requests.get(
    "https://play.3dbenbet.net/assets/main/index.283e7.js",
    headers={"User-Agent": "Mozilla/5.0"},
    timeout=120,
).text
keywords = [
    "luckydice",
    "LuckyDice",
    "CORD_INFO",
    "isAuthorized",
    "authorize",
    "ERROR_999",
    "bet=function",
    "invokeBet",
    "onClickBetTai",
    "onClickBetXiu",
    "LuckyDiceNegotiate",
    "deviceType",
    "ENTER_LOBBY",
    "sendRequestOnHub",
]
for kw in keywords:
    m = re.search(kw, t, re.I)
    if m:
        i = m.start()
        print("===", kw, "@", i, "===")
        print(t[max(0, i - 180) : i + 450])
        print()
