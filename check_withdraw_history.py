
from game_api_helper import game_request_with_retry, NODE_SERVER_URL
from datetime import datetime

def _parse_datetime(dt_str):
    if not dt_str:
        return None
    try:
        return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None

def _build_record(username, tx):
    return {
        "username": username,
        "nickname": username,  # Nếu có nickname thực thì truyền vào
        "hinhThuc": "Rút tiền",
        "transactionId": tx.get("id"),
        "amount": float(tx.get("amount", 0)),
        "time": tx.get("dateTime"),
        "status": tx.get("status"),
        "reason": tx.get("reason"),
        "content": tx.get("content"),
        "deviceNap": "",
    }

def _save_record(record):
    import requests  # Dùng requests chuẩn cho backend local
    return requests.post(f"{NODE_SERVER_URL}/api/transaction-details", json=record, timeout=5)

def _get_record(transaction_id):
    import requests  # Dùng requests chuẩn cho backend local
    if not transaction_id:
        return None
    resp = requests.get(f"{NODE_SERVER_URL}/api/transaction-details/{transaction_id}", timeout=5)
    if resp.status_code == 200:
        return resp.json()
    return None

def _update_record(record):
    import requests  # Dùng requests chuẩn cho backend local
    tx_id = record.get("transactionId")
    if not tx_id:
        return None
    return requests.put(
        f"{NODE_SERVER_URL}/api/transaction-details/{tx_id}",
        json={
            "status": record.get("status"),
            "reason": record.get("reason"),
            "content": record.get("content"),
            "amount": record.get("amount"),
            "time": record.get("time"),
        },
        timeout=5,
    )

def check_withdraw_history(
    username,
    withdraw_id=None,
    limit=10,
    max_checks=5,
    status=None,
    save_latest_only=False,
    return_details=False,
    target_tx_id=None,
    previous_status=None,
    update_if_changed=False,
    update_all_if_changed=True,
):
    """
    Sau khi rút tiền, kiểm tra lịch sử rút tiền để xác nhận trạng thái giao dịch.
    Check tối đa 5 lần với các khoảng thời gian: 30, 30, 60, 120, 240 giây.
    Nếu withdraw_id/target_tx_id được cung cấp, chỉ tìm giao dịch đó.
    Lưu giao dịch mới vào DB, tránh trùng lặp (409).
    """
    import requests  # Dùng requests chuẩn cho backend local
    # print(f"[{username}] Đang kiểm tra lịch sử rút tiền...", flush=True)
    api_url = "https://wsslot.tele68.com/v1/lobby/transaction/history"
    params = {
        "limit": limit,
        "channel_id": 2,
        "type": "WITHDRAW",
    }
    # Không lọc status
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

    # Lọc hoặc lấy giao dịch mới nhất
    if save_latest_only and transactions:
        # Ưu tiên phần tử đầu (thường newest), fallback sort theo dateTime
        latest = transactions[0]
        latest_dt = _parse_datetime(latest.get("dateTime"))
        if latest_dt is None:
            sorted_tx = sorted(
                transactions,
                key=lambda x: _parse_datetime(x.get("dateTime")) or datetime.min,
                reverse=True,
            )
            latest = sorted_tx[0] if sorted_tx else latest
        transactions_to_save = [latest]
    else:
        transactions_to_save = transactions

    # Lưu giao dịch mới vào DB, tránh trùng lặp
    saved = []
    skipped = 0
    updated = False
    updated_tx = None
    matched_tx = None
    for tx in transactions_to_save:
        if target_tx_id and tx.get("id") != target_tx_id:
            continue
        if withdraw_id and tx.get("id") != withdraw_id:
            continue
        record = _build_record(username, tx)
        try:
            resp2 = _save_record(record)
            if resp2 is not None and resp2.status_code in (200, 201):
                saved.append(record)
            elif resp2 is not None and resp2.status_code == 409:
                skipped += 1  # đã tồn tại
                if update_all_if_changed:
                    current = _get_record(record.get("transactionId"))
                    if current:
                        fields_changed = current.get("status") != record.get("status")
                        if fields_changed:
                            resp3 = _update_record(record)
                            if resp3 and resp3.status_code in (200, 204):
                                updated = True
                                updated_tx = tx
            elif resp2 is not None:
                print(
                    f"⚠️ [{username}] Lỗi lưu giao dịch {tx.get('id')} cho [{username}]: {resp2.status_code} - {resp2.text}",
                    flush=True,
                )
        except Exception as e:
            print(f"⚠️ [{username}] Lỗi lưu giao dịch {tx.get('id')} cho [{username}]: {e}", flush=True)

        if target_tx_id and tx.get("id") == target_tx_id:
            matched_tx = tx
            if update_if_changed and previous_status is not None:
                current_status = tx.get("status")
                if current_status != previous_status:
                    try:
                        resp3 = _update_record(record)
                        if resp3 and resp3.status_code in (200, 204):
                            updated = True
                            updated_tx = tx
                        else:
                            print(
                                f"⚠️ [{username}] Lỗi cập nhật giao dịch {tx.get('id')}: {resp3.status_code if resp3 else 'No response'}",
                                flush=True,
                            )
                    except Exception as e:
                        print(f"⚠️ [{username}] Lỗi cập nhật giao dịch {tx.get('id')}: {e}", flush=True)

    if saved:
        for record in saved:
            if record.get("status") == "Thành công":
                print(
                    f"💰💰💰💰💰 [{username}] Đã cập nhật trạng thái Thành công cho giao dịch rút {int(record['amount']):,}",
                    flush=True,
                )

    if updated and updated_tx and updated_tx.get("status") == "Thành công":
        print(
            f"💰💰💰💰💰 [{username}] Đã cập nhật trạng thái Thành công cho giao dịch rút {int(updated_tx.get('amount', 0)):,} ",
            flush=True,
        )

    if return_details:
        return {
            "ok": True,
            "saved_count": len(saved),
            "skipped": skipped,
            "saved": saved,
            "matched_tx": matched_tx,
            "updated": updated,
            "updated_tx": updated_tx,
            "transactions": transactions,
        }

    return len(saved) > 0

if __name__ == "__main__":
    username = input("Nhập username: ").strip()
    if not username:
        print(f"❌ Username không được để trống [{username}]")
        exit(1)
    check_withdraw_history(username)
