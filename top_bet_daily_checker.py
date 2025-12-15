# top_bet_daily_checker.py
"""
Script lấy và in ra TOP cược ngày từ 170 đến 200.
"""

from game_api_helper import game_request_with_retry, get_user_auth
import sys
from datetime import datetime

def fetch_top_bet_daily(username, date=None, limit=200):
    # Lấy access_token từ DB
    auth = get_user_auth(username)
    if not auth:
        print(f"❌ Không lấy được auth info cho {username}")
        return
    _, jwt, access_token, _ = auth
    if not access_token:
        print(f"❌ Không có access_token cho {username}")
        return
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    url = "https://gameapi.tele68.com/v1/event/top-bet/daily"
    params = {
        "date": date,
        "limit": limit,
        "cp": "R",
        "cl": "R",
        "pf": "web",
        "at": access_token
    }
    resp = game_request_with_retry(username, "GET", url, params=params)
    if not resp or resp.status_code != 200:
        print(f"❌ Lỗi lấy top cược ngày: {resp.status_code if resp else 'No response'}")
        return
    try:
        data = resp.json()
    except Exception as e:
        print(f"❌ Lỗi parse response: {e}")
        return
    if not isinstance(data, list):
        print("❌ Response không phải list")
        return
    # In ra TOP từ 170 đến 200
    print(f"\nTOP cược ngày {date} (từ 170 đến 200):\n")
    print(f"{'Idx':>4} | {'Nickname':<20} | {'MoneyBet':>12} | {'Prize':>8}")
    print("-"*55)
    for entry in data:
        idx = entry.get("idx")
        if 170 <= idx <= 200:
            nickname = entry.get("nickname", "")
            money_bet = entry.get("moneyBet", "0")
            prize = entry.get("prize", 0)
            print(f"{idx:>4} | {nickname:<20} | {int(float(money_bet)):,} | {prize:,}")

if __name__ == "__main__":
    if len(sys.argv) >= 2:
        username = sys.argv[1]
    else:
        username = input("Nhập username: ").strip()
        if not username:
            print("Chưa nhập username!")
            sys.exit(1)
    fetch_top_bet_daily(username)
