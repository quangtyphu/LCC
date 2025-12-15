"""
Get Balance API - Lấy số dư hiện tại từ game
"""
import sys
import io

# Fix encoding cho Windows console
if sys.platform == 'win32':
    import os
    os.system('chcp 65001 > nul')

from game_api_helper import game_request_with_retry, update_user_balance

BALANCE_URL = "https://gameapi.tele68.com/v1/profile/balance"


def get_balance(username: str) -> dict:
    
    # Gọi API qua helper (không cần params ngoài common params)
    resp = game_request_with_retry(username, "GET", BALANCE_URL)
    
    if not resp:
        return {"ok": False, "error": "Không gọi được API balance"}
    
    if not resp.ok:
        print(f"❌ [{username}] HTTP {resp.status_code}: {resp.text[:200]}")
        return {"ok": False, "error": f"HTTP {resp.status_code}"}
    
    try:
        data = resp.json()
        balance = data.get("balance")
        
        if balance is not None:
            print(f"✅ [{username}] Balance: {balance:,}đ")
            
            # Cập nhật balance vào DB
            update_user_balance(username, float(balance))
            
            return {
                "ok": True,
                "balance": balance,
                "username": username
            }
        else:
            print(f"❌ [{username}] API không trả về balance: {data}")
            return {"ok": False, "error": "API không trả về balance"}
    
    except Exception as e:
        print(f"❌ [{username}] Lỗi parse response: {e}")
        return {"ok": False, "error": f"Lỗi parse: {e}"}


if __name__ == "__main__":

    username = input("Nhập username: ").strip()
    if username:
        print(f"\n🔍 Đang kiểm tra balance cho [{username}]...\n")
        result = get_balance(username)
        
        if result.get("ok"):
            print(f"\n✅ Balance hiện tại: {result['balance']:,}đ")
        else:
            print(f"\n❌ Lỗi: {result.get('error')}")
    else:
        print("❌ Username không được để trống!")
