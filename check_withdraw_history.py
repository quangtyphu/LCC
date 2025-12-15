
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
    intervals = [30, 30, 60, 120, 240]
    for attempt in range(max_checks):
        print(f"[{username}] Đang kiểm tra lịch sử rút tiền (lần {attempt+1}/{max_checks})...", flush=True)
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
                    print(f"Đã lưu 1 giao dịch rút {int(tx['amount']):,} với nội dung {tx['content']}", flush=True)
                elif resp2.status_code == 409:
                    skipped += 1  # đã tồn tại
                else:
                    print(f"⚠️ [{username}] Lỗi lưu giao dịch {tx.get('id')}: {resp2.status_code} - {resp2.text}", flush=True)
            except Exception as e:
                print(f"⚠️ [{username}] Lỗi lưu giao dịch {tx.get('id')}: {e}", flush=True)

        found = False
        for tx in transactions:
            if withdraw_id is None or str(tx.get("id")) == str(withdraw_id):
                found = True
                break
        if found:
            return True
        if attempt < max_checks - 1:
            wait_time = intervals[attempt]
            print(f"⏳ Chưa thấy giao dịch thành công, đợi {wait_time}s rồi thử lại...", flush=True)
            time.sleep(wait_time)
    print(f"❌ Không tìm thấy giao dịch rút tiền {withdraw_id} thành công sau {max_checks} lần kiểm tra.", flush=True)
    return False

if __name__ == "__main__":
    username = input("Nhập username: ").strip()
    withdraw_id = input("Nhập mã giao dịch rút tiền (id): ").strip()
    if not username or not withdraw_id:
        print("❌ Username và mã giao dịch không được để trống")
        exit(1)
    result = check_withdraw_history(username, withdraw_id)
    print("Kết quả:", "Thành công" if result else "Không thành công")
