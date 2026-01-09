
import asyncio
from game_api_helper import game_request_with_retry, NODE_SERVER_URL
from get_balance import get_balance
from ws_minigame_client import connect_minigame

def check_deposit_history(username, transfer_content=None, order_id=None, amount=None, limit=10, status=None):

    """
    Lấy lịch sử nạp tiền từ game, lưu giao dịch mới vào DB, tự động nhận quà nếu đủ điều kiện.
    Sử dụng game_api_helper để lấy token, proxy, headers, params.
    """
    api_url = "https://wsslot.tele68.com/v1/lobby/transaction/history"
    params = {
        "limit": limit,
        "channel_id": 2,
        "type": "DEPOSIT",
        "status": "SUCCESS"
    }
    resp = game_request_with_retry(username, "GET", api_url, params=params)
    if not resp or resp.status_code != 200:
        print(f"❌ [{username}] Lỗi lấy lịch sử: {resp.status_code if resp else 'No response'}", flush=True)
        return {"ok": False, "error": f"Lỗi lấy lịch sử: {resp.status_code if resp else 'No response'}"}

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
        total = len(transactions)
    except Exception as e:
        print(f"❌ [{username}] Lỗi parse lịch sử: {e}", flush=True)
        return {"ok": False, "error": str(e)}

    # 2. Lưu giao dịch mới vào DB thực tế
    saved = []
    new_saved = 0
    import requests  # Dùng requests chuẩn cho backend local
    for tx in transactions:
        record = {
            "username": username,
            "nickname": username,  # Nếu có nickname thực thì truyền vào
            "hinhThuc": "Nạp tiền",
            "transactionId": tx.get("id"),
            "amount": float(tx.get("amount", 0)),
            "time": tx.get("dateTime"),
            "deviceNap": "",
        }
        try:
            resp2 = requests.post(f"{NODE_SERVER_URL}/api/transaction-details", json=record, timeout=5)
            if resp2.status_code in (200, 201):
                saved.append(record)
                new_saved += 1
                print(f"Đã lưu 1 giao dịch nạp {int(tx['amount']):,} cho [{username}] với nội dung {tx['content']}", flush=True)
                print(f"[DEBUG][{username}] Response lưu giao dịch: {resp2.text}", flush=True)
                try:
                    resp_json = resp2.json()
                    is_first = resp_json.get("isFirstDepositToday")
                    is_bonus = resp_json.get("isEligibleForBonus")
                    print(f"[INFO][{username}] isFirstDepositToday: {is_first}, isEligibleForBonus: {is_bonus}", flush=True)
                    if (is_first or is_bonus) and float(tx["amount"]) >= 200000:
                        msg = resp_json.get("message") or "Nhận quà nạp đầu tiên >= 200k!"
                        print(f"🎉 [{username}] {msg}", flush=True)
                        # Gọi nhận nhiệm vụ tự động
                        try:
                            from mission_api import auto_claim_missions
                            auto_claim_missions(username)
                        except Exception as e:
                            print(f"⚠️ [{username}] Lỗi gọi auto_claim_missions: {e}", flush=True)
                except Exception:
                    pass
            elif resp2.status_code != 409:
                print(f"⚠️ [{username}] Lỗi lưu giao dịch {tx.get('id')} cho [{username}]: {resp2.status_code} - {resp2.text}", flush=True)
        except Exception as e:
            print(f"⚠️ [{username}] Lỗi lưu giao dịch {tx.get('id')} cho [{username}]: {e}", flush=True)

    if new_saved == 0:
        print(f"Không có lệnh nạp mới nào được lưu cho [{username}]!", flush=True)
    else:
        # Khi có giao dịch mới, cập nhật balance trước khi chuyển trạng thái
        try:
            balance_result = get_balance(username)
            if balance_result.get("ok"):
                print(f"💾 [{username}] Đã cập nhật balance: {balance_result.get('balance', 'N/A')}đ", flush=True)
            else:
                print(f"⚠️ [{username}] Lỗi lấy balance: {balance_result.get('error')}", flush=True)
        except Exception as e:
            print(f"⚠️ [{username}] Lỗi khi cập nhật balance: {e}", flush=True)
        # Chuyển trạng thái sang Đang Chơi
        try:
            resp_status = requests.put(f"{NODE_SERVER_URL}/api/users/{username}", json={"status": "Đang Chơi"}, timeout=5)
            if resp_status.status_code == 200:
                print(f"🎮 [{username}] Đã chuyển trạng thái → Đang Chơi (API OK)", flush=True)
            else:
                print(f"⚠️ [{username}] Lỗi cập nhật trạng thái API: {resp_status.status_code} {resp_status.text}", flush=True)
        except Exception as e:
            print(f"⚠️ [{username}] Không kết nối được API khi update status: {e}", flush=True)

        # Sau khi nạp thành công, kết nối WS minigame 1 lần (không reconnect)
        try:
            coro = connect_minigame(username, keep_alive=False)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(coro)
            else:
                loop.create_task(coro)
            print(f"🔔 [{username}] Đã gọi WS minigame sau nạp", flush=True)
        except Exception as e:
            print(f"⚠️ [{username}] Lỗi gọi WS minigame sau nạp: {e}", flush=True)

    return {"ok": True, "total": total, "transactions": transactions}


# Cho phép chạy trực tiếp file này
if __name__ == "__main__":
    username = input("Nhập username: ").strip()
    if not username:
        print(f"❌ Username không được để trống [{username}]")
        exit(1)
    result = check_deposit_history(username)
    print(f"\nKết quả cho [{username}]:")
    print(f"[{username}] {result}")
