#!/usr/bin/env python3
import requests

t = requests.get(
    "https://play.3dbenbet.net/assets/main/index.283e7.js",
    headers={"User-Agent": "Mozilla/5.0"},
    timeout=120,
).text

def show(label: str, needle: str, ctx: int = 220) -> None:
    i = 0
    n = 0
    while n < 3:
        j = t.find(needle, i)
        if j < 0:
            break
        print(f"=== {label} #{n} @ {j} ===")
        print(t[max(0, j - ctx) : j + ctx])
        print()
        i = j + len(needle)
        n += 1

show("LuckyDiceNegotiate", "LuckyDiceNegotiate")
show("connectHubLucky", "connectHubLucky")
show("luckydiceHub", "luckydiceHub")
show("isAuthorized=!0", "isAuthorized=!0")
show("isAuthorized=!1", "isAuthorized=!1", 120)
show("onClickBetTai", "onClickBetTai")
show("onClickBetXiu", "onClickBetXiu")
show("ERROR_1001", "ERROR_1001_NOT_AUTHENTICATE")
show("enterLobby+Lucky", "LuckyDiceHub", 80)
show("negotiate", "signalr/negotiate", 100)

# bet function near BET,A
idx = t.find('M:h.BET,A:[]')
while idx >= 0:
    print("=== BET send @", idx, "===")
    print(t[max(0, idx - 80) : idx + 200])
    print()
    idx = t.find('M:h.BET,A:[]', idx + 1)
    if idx > 0 and t.find('M:h.BET,A:[]', idx) == idx:
        break
    if t.count('M:h.BET,A:[]') and idx > 520000:
        break
