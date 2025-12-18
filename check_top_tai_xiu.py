# check_top_tai_xiu.py
"""
Script lấy và in ra TOP cược Tài Xỉu theo ngày.
"""

import requests
import sys
from game_api_helper import get_user_auth

def check_top_tai_xiu(dateoffset=1, limit=10, before_id=90, access_token=None, jwt=None):
    """
    Gọi API lấy TOP cược Tài Xỉu và in ra bảng STT, idx, userName, totalWin
    """
    url = "https://wtx.tele68.com/v1/tx/daily-top"
    params = {
        "dateoffset": dateoffset,
        "limit": limit,
        "beforeId": before_id,
        "cp": "R",
        "cl": "R",
        "pf": "web",
        "at": access_token or ""
    }
    headers = {
        "Authorization": f"Bearer {jwt}" if jwt else "",
        "Referer": "https://play.lc79.bet/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        "Content-Type": "application/json"
    }
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
    except Exception as e:
        print(f"❌ Lỗi kết nối API: {e}")
        return
    if resp.status_code != 200:
        print(f"❌ Lỗi API: {resp.status_code}")
        return
    try:
        data = resp.json()
    except Exception as e:
        print(f"❌ Lỗi parse response: {e}")
        return
    tx_list = data.get("moneyTXAggList", [])
    if not isinstance(tx_list, list):
        print("❌ Không có dữ liệu moneyTXAggList")
        return
    print(f"\nTOP cược Tài Xỉu (limit={limit}, beforeId={before_id}):\n")
    print(f"{'STT':>4} | {'idx':>4} | {'userName':<20} | {'totalWin':>12}")
    print("-"*50)
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
    auth = get_user_auth(username)
    if not auth:
        print(f"❌ Không lấy được auth info cho {username}")
        return
    _, jwt, access_token, _ = auth
    if not access_token:
        print(f"❌ Không có access_token cho {username}")
        return
    # Có thể chỉnh các tham số dưới đây nếu muốn
    dateoffset = 0
    limit = 20
    before_id = 80
    check_top_tai_xiu(dateoffset=dateoffset, limit=limit, before_id=before_id, access_token=access_token, jwt=jwt)

if __name__ == "__main__":
    main()
