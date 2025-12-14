"""
Withdraw API - Rút tiền từ game về ngân hàng
"""
import requests
import time

API_BASE = "http://127.0.0.1:3000"
WITHDRAW_URL = "https://gameapi.tele68.com/v1/payment-app/cash-out/bank"

USER_AGENTS = [
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/140.0.0.0 Safari/537.36",
]

def _build_proxies(proxy_str: str):
    """Tạo dict proxies cho requests"""
    if not proxy_str:
        return None
    try:
        host, port, userp, passp = proxy_str.split(":")
        proxy_auth = f"{userp}:{passp}@{host}:{port}"
        proxy_url = f"socks5h://{proxy_auth}"
        return {"http": proxy_url, "https": proxy_url}
    except Exception:
        return None

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
        # 1. Lấy thông tin user từ DB
        resp_user = requests.get(f"{API_BASE}/api/users/{username}", timeout=5)
        if resp_user.status_code != 200:
            return {"ok": False, "error": "Không lấy được user từ DB"}
        
        user = resp_user.json()
        jwt = user.get("jwt")
        access_token = user.get("accessToken")
        proxy_str = user.get("proxy")
        
        if not jwt or not access_token:
            return {"ok": False, "error": "Thiếu JWT hoặc accessToken"}
        
        # 2. Lấy thông tin ngân hàng từ accounts (nếu chưa truyền)
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
        
        # 3. Setup proxy
        proxies = _build_proxies(proxy_str)
        
        # 4. Gửi request rút tiền
        params = {
            "cp": "R",
            "cl": "R",
            "pf": "web",
            "at": access_token
        }
        
        headers = {
            "accept": "*/*",
            "accept-language": "vi-VN,vi;q=0.9",
            "authorization": f"Bearer {jwt}",
            "cache-control": "no-cache",
            "content-type": "application/json",
            "origin": "https://play.lc79.bet",
            "referer": "https://play.lc79.bet/",
            "user-agent": USER_AGENTS[0],
        }
        
        payload = {
            "type": bank_code,
            "number": account_number,
            "name": account_holder,
            "amount": amount,
            "otp": otp
        }
        
        print(f"💸 [{username}] Đang rút {amount:,}đ về {bank_code} {account_number}...")
        
        r = requests.post(
            WITHDRAW_URL,
            params=params,
            headers=headers,
            json=payload,
            proxies=proxies,
            timeout=30
        )
        
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
            
            # Lấy balance mới (nếu có trong response)
            new_balance = data.get("data", {}).get("balance") or data.get("balance")
            
            # Cập nhật balance vào DB
            if new_balance is not None:
                try:
                    requests.put(
                        f"{API_BASE}/api/users/{username}",
                        json={"balance": new_balance},
                        timeout=5
                    )
                    print(f"💾 [{username}] Balance mới: {new_balance:,}đ")
                except Exception as e:
                    print(f"⚠️ [{username}] Không cập nhật balance: {e}")
            
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
    # Test CLI
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