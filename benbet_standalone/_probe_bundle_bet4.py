#!/usr/bin/env python3
import re
import requests

t = requests.get(
    "https://play.3dbenbet.net/assets/main/index.283e7.js",
    headers={"User-Agent": "Mozilla/5.0"},
    timeout=120,
).text

# Extract module around TaiXiuPortalView
for name in [
    "TaiXiuPortalView",
    "taiXiuPortalView",
    "connectHubTxAuthorize",
    "onLuckyDiceNegotiateResponse",
    "LuckyDiceAuthorize",
    "AuthorizeCommand",
    "enterLobby",
]:
    for m in re.finditer(re.escape(name), t):
        i = m.start()
        if name == "enterLobby" and "TaiXiu" not in t[max(0, i - 500) : i + 500]:
            continue
        print(f"=== {name} @ {i} ===")
        print(t[max(0, i - 200) : i + 600])
        print()
        break

# ServerConnector sendRequest - what params for negotiate
idx = t.find("sendRequest=function")
if idx >= 0:
    print("=== sendRequest @", idx, "===")
    print(t[idx : idx + 1200])
