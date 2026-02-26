"""
Withdraw API - Rút tiền từ game về ngân hàng
"""
import sys
import io

# Fix encoding cho Windows console
if sys.platform == 'win32':
    import os
    os.system('chcp 65001 > nul')

import requests
import time
import json
import re
from typing import Optional
from game_api_helper import game_request_with_retry, update_user_balance

API_BASE = "http://127.0.0.1:3000"
WITHDRAW_URL = "https://gameapi.tele68.com/v1/payment-app/cash-out/bank"
WITHDRAW_STATE_FILE = "queue_state.json"
WITHDRAW_LOCK_SECONDS = 15 * 60


def _load_withdraw_state() -> dict:
    try:
        with open(WITHDRAW_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
            if not isinstance(data, dict):
                return {}
            return data
    except Exception:
        return {}


def _save_withdraw_state(data: dict) -> None:
    try:
        with open(WITHDRAW_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def _get_state_with_defaults() -> dict:
    data = _load_withdraw_state()
    withdraw_queue = data.get("withdraw_queue")
    if isinstance(withdraw_queue, list):
        # migrate list -> dict by username
        migrated = {}
        for item in withdraw_queue:
            if isinstance(item, dict) and item.get("username"):
                migrated[item["username"]] = item
        data["withdraw_queue"] = migrated
    elif not isinstance(withdraw_queue, dict):
        data["withdraw_queue"] = {}

    wager_queue = data.get("wager_queue")
    if isinstance(wager_queue, list):
        migrated = {}
        for item in wager_queue:
            if isinstance(item, dict) and item.get("username"):
                migrated[item["username"]] = item.get("target_total_bet")
        data["wager_queue"] = migrated
    elif not isinstance(wager_queue, dict):
        data["wager_queue"] = {}
    return data


def _enqueue_withdraw_queue(username: str, amount: int) -> None:
    data = _get_state_with_defaults()
    queue = data.get("withdraw_queue", {})
    existing = queue.get(username)
    if isinstance(existing, dict):
        existing.setdefault("amount", amount)
        existing.setdefault("target_total_bet", None)
        existing.setdefault("added_at", time.time())
        existing.setdefault("status", "ready")
        existing.setdefault("reason", "queued")
        existing.setdefault("last_balance", None)
        existing["amount"] = amount
        queue[username] = existing
    else:
        queue[username] = {
            "amount": amount,
            "target_total_bet": None,
            "added_at": time.time(),
            "status": "ready",
            "reason": "queued",
            "last_balance": None,
        }
    data["withdraw_queue"] = queue
    _save_withdraw_state(data)


def _dequeue_withdraw_queue(username: str) -> None:
    data = _get_state_with_defaults()
    queue = data.get("withdraw_queue", {})
    if username in queue:
        del queue[username]
    data["withdraw_queue"] = queue
    _save_withdraw_state(data)


def _set_wager_queue(username: str, target_total_bet: int) -> None:
    data = _get_state_with_defaults()
    queue = data.get("wager_queue", {})
    queue[username] = int(target_total_bet)
    data["wager_queue"] = queue
    _save_withdraw_state(data)


def _clear_wager_queue(username: str) -> None:
    data = _get_state_with_defaults()
    queue = data.get("wager_queue", {})
    if username in queue:
        del queue[username]
    data["wager_queue"] = queue
    _save_withdraw_state(data)


def _get_total_bet_for_user(username: str) -> int:
    try:
        r = requests.get(
            f"{API_BASE}/api/bet-totals",
            params={"username": username},
            timeout=5
        )
        if r.status_code != 200:
            return 0
        data = r.json()
        if isinstance(data, dict):
            return int(data.get("total_all") or 0)
    except Exception:
        return 0
    return 0


def _parse_required_bet_from_error(error_message: str) -> Optional[int]:
    try:
        match = re.search(r"vui lòng chơi thêm\s+(.+)$", error_message, re.IGNORECASE)
        if match:
            raw = match.group(1)
            digits = re.sub(r"[^\d]", "", raw)
            if digits:
                return int(digits)
    except Exception:
        return None
    return None


def _get_withdraw_lock_remaining() -> int:
    data = _get_state_with_defaults()
    until_ts = data.get("locked_until")
    if not isinstance(until_ts, (int, float)):
        return 0
    remaining = int(until_ts - time.time())
    if remaining <= 0:
        data.pop("locked_until", None)
        _save_withdraw_state(data)
        return 0
    return remaining


def _set_withdraw_lock() -> None:
    data = _get_state_with_defaults()
    data["locked_until"] = time.time() + WITHDRAW_LOCK_SECONDS
    _save_withdraw_state(data)


def _parse_code(code):
    if isinstance(code, int):
        return code
    if isinstance(code, str):
        s = code.strip()
        if s.startswith("-"):
            s_num = s[1:]
            if s_num.isdigit():
                return -int(s_num)
        if s.isdigit():
            return int(s)
    return code

def _post_withdraw_check_async(username: str, delay_seconds: int = 3) -> None:
    def _run():
        try:
            time.sleep(delay_seconds)
            try:
                from get_balance import get_balance as get_balance_func
                balance_result = get_balance_func(username)
                if isinstance(balance_result, dict) and balance_result.get("ok"):
                    balance_value = balance_result.get("balance")
                    if balance_value is not None:
                        print(f"✅ [{username}] get_balance OK: {balance_value}", flush=True)
            except Exception as e:
                print(f"⚠️ [{username}] Lỗi get_balance sau lỗi rút: {e}", flush=True)

            try:
                from check_withdraw_history import check_withdraw_history
                check_withdraw_history(username, limit=10)
            except Exception as e:
                print(f"⚠️ [{username}] Lỗi check lịch sử rút sau lỗi rút: {e}", flush=True)
        except Exception:
            pass

    import threading
    threading.Thread(target=_run, daemon=True).start()

def withdraw(
    username: str,
    amount: int,
    bank_code: str = None,
    account_number: str = None,
    account_holder: str = None,
    otp: str = ""
) -> dict:
    """
    Rút tiền từ game về ngân hàng.
    
    Args:
        username: Username trong DB
        amount: Số tiền rút (VNĐ)
        bank_code: Mã ngân hàng (VD: VPB, MB, TCB...) - nếu None sẽ lấy từ DB
        account_number: Số tài khoản - nếu None sẽ lấy từ DB
        account_holder: Tên chủ tài khoản - nếu None sẽ lấy từ DB
        otp: Mã OTP (nếu cần, mặc định rỗng)
    
    Returns:
        {"ok": True, "message": "...", "balance": 123456} hoặc {"ok": False, "error": "..."}
    """
    try:
        # Check lock rút tiền (từ lỗi code -2)
        remaining = _get_withdraw_lock_remaining()
        if remaining > 0:
            mins = (remaining + 59) // 60
            _enqueue_withdraw_queue(username, amount)
            return {
                "ok": False,
                "error": f"Rút tiền đang bị khóa, vui lòng thử lại sau ~{mins} phút"
            }

        # Lấy thông tin ngân hàng từ accounts (nếu chưa truyền)
        if not bank_code or not account_number or not account_holder:
            resp_acc = requests.get(f"{API_BASE}/api/accounts/{username}", timeout=5)
            if resp_acc.status_code != 200:
                return {"ok": False, "error": "Không lấy được account từ DB"}
            
            account = resp_acc.json()
            bank_code = bank_code or account.get("bank")
            account_number = account_number or account.get("accountNumber")
            account_holder = account_holder or account.get("accountHolder")
        
        if not bank_code or not account_number or not account_holder:
            return {"ok": False, "error": "Thiếu thông tin ngân hàng"}
        
        # Payload rút tiền
        payload = {
            "type": bank_code,
            "number": account_number,
            "name": account_holder,
            "amount": amount,
            "otp": otp
        }
        
        # Bỏ log bắt đầu rút để tránh trùng log
        
        # Gọi API qua helper
        r = game_request_with_retry(username, "POST", WITHDRAW_URL, json_data=payload)
        
        if not r:
            _post_withdraw_check_async(username, delay_seconds=3)
            return {"ok": False, "error": "Không gọi được API rút tiền"}
        
        if not r.ok:
            print(f"❌ [withdraw][{username}] HTTP {r.status_code}: {r.text[:200]}")
            return {"ok": False, "error": f"HTTP {r.status_code}"}
        
        data = r.json()
        
        # Parse response
        code = data.get("code")
        code_int = _parse_code(code)
        message = data.get("message")
        
        # Chỉ log một dòng theo yêu cầu
        try:
            import re
            amount_line = None
            if message:
                for line in str(message).splitlines():
                    if "Số tiền rút" in line:
                        amount_line = line.strip()
                        break
            if amount_line:
                print(f"💰💰💰💰💰 [{username}] {amount_line} 💰💰💰💰💰 Mã Code: {code}", flush=True)
            else:
                print(f"💰💰💰💰💰 [{username}] Số tiền rút: {amount:,}₫ 💰💰💰💰💰 Mã Code: {code}", flush=True)
        except Exception:
            print(f"💰💰💰💰💰 [{username}] Số tiền rút: {amount:,}₫ 💰💰💰💰💰 Mã Code: {code}", flush=True)
        
        # Code 0 và 1 đều là thành công (1 = đợi xử lý, 0 = thành công ngay)
        if code_int in [0, 1]:
            # Thành công
            # Không log tại đây để tránh trùng log
            # Lấy balance mới (ưu tiên data.balance, sau đó đến data.current_money)
            new_balance = (
                data.get("data", {}).get("balance")
                or data.get("balance")
                or data.get("current_money")
            )
            # Cập nhật balance vào DB nếu có trong response
            if new_balance is not None:
                update_user_balance(username, float(new_balance))
                # Bỏ log balance để tránh trùng log
            else:
                # Nếu response không có balance (code 1), gọi get_balance để lấy
                try:
                    from get_balance import get_balance as get_balance_func
                    balance_result = get_balance_func(username)
                    if balance_result.get("ok"):
                        new_balance = balance_result.get("balance")
                except Exception as e:
                    print(f"⚠️ [{username}] Không lấy được balance: {e}")

            # Chạy check_withdraw_history ở background để không chặn luồng chính
            def _check_withdraw_history_async():
                try:
                    from check_withdraw_history import check_withdraw_history
                    latest_tx_id = None
                    latest_status = None
                    time.sleep(2)
                    initial_result = check_withdraw_history(
                        username,
                        limit=20,
                        status=None,
                        save_latest_only=True,
                        return_details=True,
                    )
                    transactions = initial_result.get("transactions") or []
                    if transactions:
                        latest_tx = transactions[0]
                        latest_tx_id = latest_tx.get("id")
                        latest_status = latest_tx.get("status")

                    # Định kỳ như cũ để check lại trạng thái giao dịch
                    intervals = [40, 30,30,30,30, 30,30,60, 60,60,120,120,120]
                    found = latest_tx_id is not None
                    for wait_time in intervals:
                        time.sleep(wait_time)
                        if not latest_tx_id:
                            continue
                        result = check_withdraw_history(
                            username,
                            limit=10,
                            status=None,
                            target_tx_id=latest_tx_id,
                            previous_status=latest_status,
                            update_if_changed=True,
                            return_details=True,
                        )
                        matched = result.get("matched_tx")
                        if matched:
                            current_status = matched.get("status")
                            if current_status != latest_status:
                                latest_status = current_status
                        if result.get("saved_count", 0) > 0:
                            found = True
                    # Không cần else log nữa

                    # Nếu không có balance mới từ response, sau khi phát hiện giao dịch thành công thì lấy balance mới nhất từ DB hoặc API game và cập nhật vào DB
                    if found and new_balance is None:
                        try:
                            # Gọi API game để lấy balance mới nhất
                            from get_balance import get_balance
                            balance = get_balance(username)
                            if balance is not None:
                                update_user_balance(username, float(balance))
                        except Exception as e:
                            print(f"[AutoCheck][{username}] Lỗi cập nhật balance sau khi rút tiền: {e}")
                except Exception as e:
                    print(f"[AutoCheck][{username}] Lỗi khi kiểm tra lịch sử rút tiền: {e}")

            import threading
            threading.Thread(target=_check_withdraw_history_async, daemon=True).start()

            _dequeue_withdraw_queue(username)
            _clear_wager_queue(username)

            return {
                "ok": True,
                "message": message or "Rút tiền thành công",
                "balance": new_balance,
                "response": data
            }
        else:
            # Lỗi
            print(f"❌ [{username}] Rút tiền thất bại: [{code}] {message}")
            if code_int == -1111:
                _post_withdraw_check_async(username, delay_seconds=3)
            if code_int == -2:
                _set_withdraw_lock()
                _enqueue_withdraw_queue(username, amount)
            if code_int == -10:
                need_more = _parse_required_bet_from_error(str(message or ""))
                if need_more:
                    current_total = _get_total_bet_for_user(username)
                    target_total_bet = current_total + need_more
                    _set_wager_queue(username, target_total_bet)
            return {
                "ok": False,
                "error": f"[{code}] {message}",
                "response": data
            }
    
    except Exception as e:
        print(f"❌ [{username}] Lỗi rút tiền: {e}")
        import traceback
        traceback.print_exc()
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    import sys
    import json
    
    # Nếu có arguments từ command line -> mode API (trả JSON)
    if len(sys.argv) >= 3:
        try:
            username = sys.argv[1]
            amount = int(sys.argv[2])
            
            # Parse optional arguments
            bank_code = None
            account_number = None
            account_holder = None
            otp = ""
            
            i = 3
            while i < len(sys.argv):
                if sys.argv[i] == '--bank' and i + 1 < len(sys.argv):
                    bank_code = sys.argv[i + 1]
                    i += 2
                elif sys.argv[i] == '--account' and i + 1 < len(sys.argv):
                    account_number = sys.argv[i + 1]
                    i += 2
                elif sys.argv[i] == '--holder' and i + 1 < len(sys.argv):
                    account_holder = sys.argv[i + 1]
                    i += 2
                elif sys.argv[i] == '--otp' and i + 1 < len(sys.argv):
                    otp = sys.argv[i + 1]
                    i += 2
                else:
                    i += 1
            
            result = withdraw(username, amount, bank_code, account_number, account_holder, otp)
            
            # In ra JSON để Node.js đọc
            print(f"[withdraw][{username}] {json.dumps(result, ensure_ascii=False)}")
            sys.exit(0 if result.get('ok') else 1)
            
        except Exception as e:
            print(f"[withdraw][{username}] {json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False)}")
            sys.exit(1)
    
    # Mode interactive (không có arguments)
    else:
        username = input("👤 Username: ").strip()
        amount = int(input("💰 Số tiền rút: ").strip())
        
        result = withdraw(username, amount)
        
        if result["ok"]:
            pass
        else:
            print(f"\n❌ [{username}] Thất bại: {result['error']}")