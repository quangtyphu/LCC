# top_bet_daily_checker.py
"""
Script lấy và in ra TOP cược ngày ở các khoảng 180-200 và 480-500.
"""

from game_api_helper import game_request_with_retry
from constants import load_config
import sys
from datetime import datetime
import requests

API_BASE = "http://127.0.0.1:3000"


def _to_int(val, default=0):
    try:
        return int(float(val))
    except Exception:
        return default


def _fetch_closest_users_below_target(target_amount: int, take: int = 8):
    if target_amount <= 0 or take <= 0:
        return []
    try:
        r = requests.get(f"{API_BASE}/api/bet-totals", params={"page": 1, "limit": 10000}, timeout=8)
        if r.status_code != 200:
            return []
        payload = r.json()
        items = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            return []

        candidates = []
        for row in items:
            username = str(row.get("username") or row.get("user") or "").strip()
            if not username:
                continue
            total_day = _to_int(
                row.get("total_day")
                or row.get("today_bet")
                or row.get("todayBet")
                or row.get("total")
                or row.get("totalBet"),
                0,
            )
            if 0 < total_day < target_amount:
                candidates.append(
                    {
                        "username": username,
                        "total_day": total_day,
                        "gap": target_amount - total_day,
                    }
                )
        candidates.sort(key=lambda x: x["total_day"], reverse=True)
        return candidates[:take]
    except Exception:
        return []


def _get_v2_target_count(default_count: int = 8) -> int:
    cfg = load_config() or {}
    mode = cfg.get("AUTO_REFRESH_V2_FROM_TOP480", {})
    if isinstance(mode, dict):
        try:
            count = int(mode.get("V2_COUNT", default_count) or default_count)
            if count > 0:
                return count
        except Exception:
            pass

    v2_list = cfg.get("PRIORITY_USERS_V2", [])
    if isinstance(v2_list, list):
        normalized = [str(u or "").strip() for u in v2_list if str(u or "").strip()]
        if normalized:
            return len(normalized)

    return default_count


def fetch_top_bet_daily(username, date=None, limit=500, nearest_users_count=None):
    if nearest_users_count is None:
        nearest_users_count = _get_v2_target_count(default_count=8)

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

    # Lấy mốc top 480 (moneyBet của idx=480), rồi in thêm N user có total_day gần dưới nhất.
    target_top_480 = None
    for entry in data:
        idx = _to_int(entry.get("idx"), 0)
        if idx == 480:
            target_top_480 = _to_int(entry.get("moneyBet"), 0)
            break

    if not target_top_480:
        print("\n⚠️ Không lấy được mốc idx=480 từ dữ liệu top-bet ngày (có thể do API giới hạn limit).")
        return

    closest = _fetch_closest_users_below_target(target_top_480, take=nearest_users_count)
    print(f"\n{nearest_users_count} user có tổng cược ngày gần dưới mốc top 480 ({target_top_480:,}):\n")
    print(f"{'No':>3} | {'Username':<20} | {'TotalDay':>12} | {'Gap':>12}")
    print("-" * 60)
    if not closest:
        print("⚠️ Không có dữ liệu phù hợp từ API /api/bet-totals.")
        return
    for i, row in enumerate(closest, 1):
        print(
            f"{i:>3} | {row['username']:<20} | {row['total_day']:>12,} | {row['gap']:>12,}"
        )

if __name__ == "__main__":
    if len(sys.argv) >= 2:
        username = sys.argv[1]
    else:
        username = input("Nhập username: ").strip()
        if not username:
            print("Chưa nhập username!")
            sys.exit(1)
    fetch_top_bet_daily(username)
