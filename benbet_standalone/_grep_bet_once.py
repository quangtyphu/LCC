import requests

t = requests.get(
    "https://play.3dbenbet.net/assets/main/index.283e7.js",
    headers={"User-Agent": "Mozilla/5.0"},
    timeout=120,
).text
for kw in [
    "enterLobby=function",
    "bet=function(e,t)",
    "invoke(\"bet\"",
    "onClickBetTai",
    "onClickBetXiu",
    "BetSide",
    "sendRequestOnHub",
]:
    i = t.find(kw)
    if i >= 0:
        print("===", kw, "===")
        print(t[max(0, i - 120) : i + 320])
        print()
