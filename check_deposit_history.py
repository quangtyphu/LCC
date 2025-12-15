import requests
import json

def check_deposit_history(username, transfer_content=None, order_id=None, amount=None, limit=10, status=None):
    """
    Lấy lịch sử nạp tiền từ game, lưu giao dịch mới vào DB, tự động nhận quà nếu đủ điều kiện.
    """
    print(f"📋 [{username}] Đang lấy lịch sử nạp tiền (limit={limit}, status={status})...", flush=True)
    # 1. Gọi API game lấy lịch sử nạp tiền
    # (Giả lập, bạn cần thay bằng logic thực tế)
    try:
        # Ví dụ: response = requests.get(...)
        # Giả lập kết quả
        transactions = [
            {"id": 1, "amount": 250000, "content": "NDCK123", "status": "success"},
        ]
        total = len(transactions)
        print(f"✅ [{username}] Tìm thấy {total} giao dịch", flush=True)
    except Exception as e:
        print(f"❌ [{username}] Lỗi lấy lịch sử: {e}", flush=True)
        return {"ok": False, "error": str(e)}

    # 2. Lưu giao dịch mới vào DB
    for tx in transactions:
        try:
            # Gọi API backend lưu giao dịch (giả lập)
            # resp = requests.post(...)
            print(f"✅ [{username}] Lưu 1 giao dịch Nạp Tiền với số tiền là: {tx['amount']:,}đ với NDCK là: {tx['content']}", flush=True)
        except Exception as e:
            print(f"⚠️ [{username}] Lỗi lưu giao dịch {tx.get('id')}: {e}", flush=True)

    # 3. Nếu là nạp đầu tiên trong ngày >= 200k thì nhận quà
    # (Giả lập, bạn cần thay bằng logic thực tế)
    if transactions and transactions[0]["amount"] >= 200000:
        print(f"🎉 [{username}] Nhận quà nạp đầu tiên >= 200k!", flush=True)

    # 4. Cập nhật trạng thái user sang Đang Chơi
    print(f"🎮 [{username}] Đã chuyển trạng thái → Đang Chơi", flush=True)

    return {"ok": True, "total": total, "transactions": transactions}
