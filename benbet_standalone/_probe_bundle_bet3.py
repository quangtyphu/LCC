#!/usr/bin/env python3
import requests

t = requests.get(
    "https://play.3dbenbet.net/assets/main/index.283e7.js",
    headers={"User-Agent": "Mozilla/5.0"},
    timeout=120,
).text

def show(label: str, needle: str, ctx: int = 280) -> None:
    i = t.find(needle)
    if i < 0:
        print(f"--- {label}: NOT FOUND ---")
        return
    print(f"=== {label} @ {i} ===")
    print(t[max(0, i - ctx) : i + ctx])
    print()

show("connectHubTx", "connectHubTx")
show("connectHubTxAuthorize", "connectHubTxAuthorize")
show("connectHubLucky", "connectHubLucky")
show("LuckyDiceConnect", "LuckyDiceConnect")
show("onLuckyDiceNegotiate", "onLuckyDiceNegotiate")
show("enterLobby case Lucky", "case o.LuckyDiceHub")
show("BET LuckyDice", "LuckyDiceHub:t.A")
show("hub connect this", ".connect(this,I.LuckyDiceHub")

# full bet switch for luckydice
idx = t.find("n.bet=function")
for k in range(5):
    if idx < 0:
        break
    if k == 0 or "LuckyDice" in t[idx : idx + 800]:
        print(f"=== n.bet=function #{k} @ {idx} ===")
        print(t[idx : idx + 900])
        print()
    idx = t.find("n.bet=function", idx + 1)
