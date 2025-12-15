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
from game_api_helper import game_request_with_retry, update_user_balance

API_BASE = "http://127.0.0.1:3000"
WITHDRAW_URL = "https://gameapi.tele68.com/v1/payment-app/cash-out/bank"

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
        
        print(f"💸 [{username}] Đang rút {amount:,}đ về {bank_code} {account_number}...")
        
        # Gọi API qua helper
        r = game_request_with_retry(username, "POST", WITHDRAW_URL, json_data=payload)
        
        if not r:
            return {"ok": False, "error": "Không gọi được API rút tiền"}
        
        if not r.ok:
            print(f"❌ [{username}] HTTP {r.status_code}: {r.text[:200]}")
            return {"ok": False, "error": f"HTTP {r.status_code}"}
        
        data = r.json()
        
        # Parse response
        code = data.get("code")
        message = data.get("message")
        
        if code == 0:
            # Thành công
            print(f"✅ [{username}] Rút tiền thành công!")
            # Lấy balance mới (ưu tiên data.balance, sau đó đến data.current_money)
            new_balance = (
                data.get("data", {}).get("balance")
                or data.get("balance")
                or data.get("current_money")
            )
            # Cập nhật balance vào DB nếu có trong response
            if new_balance is not None:
                update_user_balance(username, float(new_balance))
                print(f"💾 [{username}] Balance mới: {new_balance:,}đ")

            # Gọi check_withdraw_history định kỳ cho đến khi có giao dịch mới được lưu
            try:
                from check_withdraw_history import check_withdraw_history
                intervals = [30, 30, 60, 120, 240]
                found = False
                for idx, wait_time in enumerate(intervals):
                    print(f"[AutoCheck] Đang kiểm tra lịch sử rút tiền (lần {idx+1}/{len(intervals)})...")
                    result = check_withdraw_history(username, limit=20, max_checks=1)
                    if result:
                        print(f"[AutoCheck] Đã phát hiện giao dịch rút tiền mới, dừng kiểm tra.")
                        found = True
                        break
                    if idx < len(intervals) - 1:
                        print(f"[AutoCheck] Chưa có giao dịch mới, đợi {wait_time}s...")
                        time.sleep(wait_time)
                else:
                    print(f"[AutoCheck] Không phát hiện giao dịch rút tiền mới sau {len(intervals)} lần kiểm tra.")

                # Nếu không có balance mới từ response, sau khi phát hiện giao dịch thành công thì lấy balance mới nhất từ DB hoặc API game và cập nhật vào DB
                if found and new_balance is None:
                    try:
                        # Gọi API game để lấy balance mới nhất
                        from get_balance import get_balance
                        balance = get_balance(username)
                        if balance is not None:
                            update_user_balance(username, float(balance))
                            print(f"💾 [{username}] Balance mới (sau check): {balance:,}đ")
                    except Exception as e:
                        print(f"[AutoCheck] Lỗi cập nhật balance sau khi rút tiền: {e}")
            except Exception as e:
                print(f"[AutoCheck] Lỗi khi kiểm tra lịch sử rút tiền: {e}")

            return {
                "ok": True,
                "message": message or "Rút tiền thành công",
                "balance": new_balance,
                "response": data
            }
        else:
            # Lỗi
            print(f"❌ [{username}] Rút tiền thất bại: [{code}] {message}")
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
            print(json.dumps(result, ensure_ascii=False))
            sys.exit(0 if result.get('ok') else 1)
            
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            sys.exit(1)
    
    # Mode interactive (không có arguments)
    else:
        username = input("👤 Username: ").strip()
        amount = int(input("💰 Số tiền rút: ").strip())
        
        result = withdraw(username, amount)
        
        if result["ok"]:
            print(f"\n✅ Thành công!")
            print(f"   Message: {result['message']}")
            if result.get("balance"):
                print(f"   Balance mới: {result['balance']:,}đ")
        else:
            print(f"\n❌ Thất bại: {result['error']}")