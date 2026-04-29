# top_bet_daily_checker.py
"""
Script lấy và in ra TOP cược ngày ở các khoảng 180-200 và 480-500.
"""

from game_api_helper import game_request_with_retry
import sys
from datetime import datetime

def fetch_top_bet_daily(username, date=None, limit=500):
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    url = "https://gameapi.tele68.com/v1/event/top-bet/daily"
    params = {
        "date": date,
        "limit": limit,
    }
    top_bet_headers = {
        # Endpoint này nhạy với origin/referer, dùng giống request browser bạn cung cấp.
        "origin": "https://lc79b.bet",
        "referer": "https://lc79b.bet/",
        "accept-language": "vi-VN,vi;q=0.9,fr-FR;q=0.8,fr;q=0.7,en-US;q=0.6,en;q=0.5",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    }
    resp = game_request_with_retry(username, "GET", url, params=params, extra_headers=top_bet_headers)
    if not resp:
        print("❌ Lỗi lấy top cược ngày: No response")
        return
    if resp.status_code == 400 and limit > 200:
        fallback_limit = 200
        print(f"⚠️ API không nhận limit={limit}, thử lại với limit={fallback_limit}...")
        params["limit"] = fallback_limit
        resp = game_request_with_retry(username, "GET", url, params=params, extra_headers=top_bet_headers)
    if not resp or resp.status_code != 200:
        body_preview = ""
        if resp is not None:
            try:
                body_preview = f" | body={resp.text[:200]}"
            except Exception:
                body_preview = ""
        print(f"❌ Lỗi lấy top cược ngày: {resp.status_code if resp else 'No response'}{body_preview}")
        return
    try:
        data = resp.json()
    except Exception as e:
        print(f"❌ Lỗi parse response: {e}")
        return
    if not isinstance(data, list):
        print("❌ Response không phải list")
        return
    # In ra TOP ở 2 khoảng: 180-200 và 480-500
    print(f"\nTOP cược ngày {date} (180-200 và 480-500):\n")
    print(f"{'Idx':>4} | {'Nickname':<20} | {'MoneyBet':>12} | {'Prize':>8}")
    print("-"*55)
    printed_count = 0
    for entry in data:
        idx = entry.get("idx")
        try:
            idx_int = int(idx)
        except Exception:
            continue
        if (180 <= idx_int <= 200) or (480 <= idx_int <= 500):
            nickname = entry.get("nickname", "")
            money_bet = entry.get("moneyBet", "0")
            prize = entry.get("prize", 0)
            print(f"{idx_int:>4} | {nickname:<20} | {int(float(money_bet)):,} | {prize:,}")
            printed_count += 1
    if printed_count == 0:
        print("⚠️ Không có dòng nào trong 2 khoảng yêu cầu từ dữ liệu API trả về.")

if __name__ == "__main__":
    if len(sys.argv) >= 2:
        username = sys.argv[1]
    else:
        username = input("Nhập username: ").strip()
        if not username:
            print("Chưa nhập username!")
            sys.exit(1)
    fetch_top_bet_daily(username)
