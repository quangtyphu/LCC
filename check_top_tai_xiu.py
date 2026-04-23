# check_top_tai_xiu.py
"""
Script lấy và in ra TOP cược Tài Xỉu theo ngày.
Gọi API qua game_request_with_retry → luôn đi SOCKS5 proxy của user trong DB.
"""

import sys

from game_api_helper import game_request_with_retry

DAILY_TOP_URL = "https://wtx.tele68.com/v1/tx/daily-top"


def check_top_tai_xiu(username: str, dateoffset=1, limit=10, before_id=90):
    """
    Gọi API lấy TOP cược Tài Xỉu và in ra bảng STT, idx, userName, totalWin.
    """
    params = {
        "dateoffset": dateoffset,
        "limit": limit,
        "beforeId": before_id,
    }
    resp = game_request_with_retry(
        username, "GET", DAILY_TOP_URL, params=params, timeout=20
    )
    if not resp:
        print(f"❌ [{username}] Không có response (proxy/token/lỗi mạng)")
        return
    if resp.status_code != 200:
        print(f"❌ [{username}] Lỗi API game: HTTP {resp.status_code}")
        return
    try:
        data = resp.json()
    except Exception as e:
        print(f"❌ [{username}] Lỗi parse response: {e}")
        return
    tx_list = data.get("moneyTXAggList", [])
    if not isinstance(tx_list, list):
        print("❌ Không có dữ liệu moneyTXAggList")
        return
    print(f"\nTOP cược Tài Xỉu (limit={limit}, beforeId={before_id}):\n")
    print(f"{'STT':>4} | {'idx':>4} | {'userName':<20} | {'totalWin':>12}")
    print("-" * 50)
    for stt, entry in enumerate(tx_list, 1):
        idx = entry.get("idx", "")
        user = entry.get("userName", "")
        total_win = entry.get("totalWin", 0)
        print(f"{stt:>4} | {idx:>4} | {user:<20} | {total_win:,}")


def main():
    if len(sys.argv) >= 2:
        username = sys.argv[1]
    else:
        username = input("Nhập username: ").strip()
        if not username:
            print("Chưa nhập username!")
            sys.exit(1)
    dateoffset = 0
    limit = 30
    before_id = 90
    check_top_tai_xiu(username, dateoffset=dateoffset, limit=limit, before_id=before_id)


if __name__ == "__main__":
    main()
