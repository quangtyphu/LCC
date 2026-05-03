# check_top_tai_xiu.py
"""
Script lấy và in ra TOP cược Tài Xỉu theo ngày.
Gọi API qua game_request_with_retry → luôn đi SOCKS5 proxy của user trong DB.
"""

import sys

from game_api_helper import game_request_with_retry

DAILY_TOP_URL = "https://wtx.tele68.com/v1/tx/daily-top"


def check_top_tai_xiu(
    username: str,
    dateoffset=1,
    limit=10,
    before_id=None,
    top_from: int | None = None,
    top_to: int | None = None,
):
    """
    Gọi API lấy TOP cược Tài Xỉu và in ra bảng STT, idx, userName, totalWin.
    """
    tx_list = []
    if top_from is not None and top_to is not None:
        # API daily-top thực tế trả tối đa ~20 dòng / lần; lấy theo từng block idx.
        try:
            start_idx = max(1, int(top_from))
            end_idx = max(start_idx, int(top_to))
        except Exception:
            start_idx, end_idx = 1, 0

        seen_idx = set()
        cursor = start_idx
        while cursor <= end_idx:
            params = {
                "dateoffset": dateoffset,
                "limit": max(20, int(limit)),
                "beforeId": max(0, cursor - 1),
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

            page_rows = data.get("moneyTXAggList", [])
            if not isinstance(page_rows, list):
                print("❌ Không có dữ liệu moneyTXAggList")
                return
            if not page_rows:
                break

            for row in page_rows:
                try:
                    idx_val = int(row.get("idx"))
                except Exception:
                    continue
                if start_idx <= idx_val <= end_idx and idx_val not in seen_idx:
                    tx_list.append(row)
                    seen_idx.add(idx_val)
            cursor += 20
    else:
        params = {
            "dateoffset": dateoffset,
            "limit": limit,
        }
        if before_id is not None:
            params["beforeId"] = before_id
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

    if not isinstance(tx_list, list):
        print("❌ Không có dữ liệu moneyTXAggList")
        return
    if top_from is not None and top_to is not None:
        print(
            f"\nTOP cược Tài Xỉu (từ {top_from} đến {top_to}, limit={limit}, beforeId={before_id}):\n"
        )
    else:
        print(f"\nTOP cược Tài Xỉu (limit={limit}, beforeId={before_id}):\n")
    print(f"{'STT':>4} | {'idx':>4} | {'userName':<20} | {'totalWin':>12}")
    print("-" * 50)
    rows = tx_list
    if top_from is not None and top_to is not None:
        try:
            rows = sorted(tx_list, key=lambda x: int(x.get("idx", 0)))
        except Exception:
            rows = tx_list

    shown = 0
    start_rank = int(top_from) if top_from is not None else 1
    for i, entry in enumerate(rows, 0):
        idx = entry.get("idx", "")
        shown_rank = start_rank + i
        user = entry.get("userName", "")
        total_win = entry.get("totalWin", 0)
        print(f"{shown_rank:>4} | {idx:>4} | {user:<20} | {total_win:,}")
        shown += 1
    if shown == 0:
        print(f"⚠️ Không có dữ liệu trong khoảng TOP yêu cầu. (API trả {len(tx_list)} dòng)")


def main():
    if len(sys.argv) >= 2:
        username = sys.argv[1]
    else:
        username = input("Nhập username: ").strip()
        if not username:
            print("Chưa nhập username!")
            sys.exit(1)
    dateoffset = 0
    top_from = 70
    top_to = 110
    limit = 120
    before_id = None
    check_top_tai_xiu(
        username,
        dateoffset=dateoffset,
        limit=limit,
        before_id=before_id,
        top_from=top_from,
        top_to=top_to,
    )


if __name__ == "__main__":
    main()
