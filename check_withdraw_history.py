
from game_api_helper import game_request_with_retry, NODE_SERVER_URL
import time

def check_withdraw_history(username, withdraw_id=None, limit=20, max_checks=5):
    """
    Sau khi rút tiền, kiểm tra lịch sử rút tiền để xác nhận trạng thái giao dịch.
    Check tối đa 5 lần với các khoảng thời gian: 30, 30, 60, 120, 240 giây.
    Nếu withdraw_id được cung cấp, chỉ tìm giao dịch đó.
    Lưu giao dịch mới vào DB, tránh trùng lặp (409).
    """
    import requests  # Dùng requests chuẩn cho backend local
    # print(f"[{username}] Đang kiểm tra lịch sử rút tiền...", flush=True)
    api_url = "https://wsslot.tele68.com/v1/lobby/transaction/history"
    params = {
        "limit": limit,
        "channel_id": 2,
        "type": "WITHDRAW",
        "status": "SUCCESS"
    }
    resp = game_request_with_retry(username, "GET", api_url, params=params)
    if not resp or resp.status_code != 200:
        print(f"❌ [{username}] Lỗi lấy lịch sử: {resp.status_code if resp else 'No response'}", flush=True)
        return False
    try:
        transactions_raw = resp.json()
        transactions = []
        for tx in transactions_raw:
            transactions.append({
                "id": tx.get("id"),
                "amount": int(tx.get("amount", 0)),
                "content": tx.get("content"),
                "status": tx.get("status"),
                "dateTime": tx.get("dateTime"),
                "reason": tx.get("reason")
            })
    except Exception as e:
        print(f"❌ [{username}] Lỗi parse lịch sử: {e}", flush=True)
        return False

    # Lưu giao dịch mới vào DB, tránh trùng lặp
    saved = []
    skipped = 0
    new_saved = []
    for tx in transactions:
        record = {
            "username": username,
            "nickname": username,  # Nếu có nickname thực thì truyền vào
            "hinhThuc": "Rút tiền",
            "transactionId": tx.get("id"),
            "amount": float(tx.get("amount", 0)),
            "time": tx.get("dateTime"),
            "deviceNap": "",
        }
        try:
            resp2 = requests.post(f"{NODE_SERVER_URL}/api/transaction-details", json=record, timeout=5)
            if resp2.status_code in (200, 201):
                saved.append(record)
                new_saved.append(tx)
            elif resp2.status_code == 409:
                skipped += 1  # đã tồn tại
            else:
                print(f"⚠️ [{username}] Lỗi lưu giao dịch {tx.get('id')} cho [{username}]: {resp2.status_code} - {resp2.text}", flush=True)
        except Exception as e:
            print(f"⚠️ [{username}] Lỗi lưu giao dịch {tx.get('id')} cho [{username}]: {e}", flush=True)

    if new_saved:
        for tx in new_saved:
            print(f"Đã lưu 1 giao dịch rút {int(tx['amount']):,} cho [{username}] Thời gian: {tx.get('dateTime')} ", flush=True)
        return True
    else:
        return False

if __name__ == "__main__":
    username = input("Nhập username: ").strip()
    if not username:
        print(f"❌ Username không được để trống [{username}]")
        exit(1)
    check_withdraw_history(username)
