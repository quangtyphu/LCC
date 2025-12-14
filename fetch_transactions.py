import asyncio
import requests
from datetime import datetime

# API server Node của bạn (CMS local)
NODE_SERVER_URL = "http://127.0.0.1:3000"   # đổi thành IP nếu cần
HISTORY_URL = "https://wsslot.tele68.com/v1/lobby/transaction/history"


async def fetch_transactions_async(username: str, tx_type: str = "DEPOSIT", limit: int = 50):
    """
    Lấy giao dịch từ tele68 → lưu vào DB local (kiểm tra trùng qua status 409).
    """
    try:
        # 1) Lấy proxy + JWT từ DB local
        user_resp = await asyncio.to_thread(
            lambda: requests.get(f"{NODE_SERVER_URL}/api/users/{username}", timeout=5)
        )
        if user_resp.status_code != 200:
            return []
        
        user_doc = user_resp.json()
        proxy_str = user_doc.get("proxy")
        jwt = user_doc.get("jwt")
        access_token = user_doc.get("accessToken")
        nickname = user_doc.get("nickname", "")
        
        if not proxy_str or not jwt or not access_token:
            return []
        
        # 2) Parse proxy
        try:
            host, port, userp, passp = proxy_str.split(":")
            proxy_auth = f"{userp}:{passp}@{host}:{port}"
            proxy_url = f"socks5h://{proxy_auth}"
            proxies = {"http": proxy_url, "https": proxy_url}
        except Exception:
            return []
        
        # 3) Gọi API tele68
        params = {
            "limit": limit,
            "channel_id": 2,
            "type": tx_type,
            "status": "SUCCESS",
            "cp": "R",
            "cl": "R",
            "pf": "web",
            "at": access_token,
        }
        headers = {"Authorization": f"Bearer {jwt}", "Accept": "application/json"}
        
        resp = await asyncio.to_thread(
            lambda: requests.get(
                HISTORY_URL,
                params=params,
                headers=headers,
                proxies=proxies,
                timeout=20
            )
        )
        
        if not resp.ok:
            return []
        
        data = resp.json()
        if isinstance(data, dict):
            data = data.get("data", [])
        
        saved = []
        skipped = 0
        
        # 4) Lưu từng giao dịch (kiểm tra trùng qua 409)
        for tx in data:
            transaction_id = tx.get("id")
            amount = float(tx.get("amount", 0))
            try:
                tx_time = datetime.strptime(tx.get("dateTime"), "%Y-%m-%d %H:%M:%S").isoformat()
            except Exception:
                tx_time = tx.get("dateTime")
            
            record = {
                "username": username,
                "nickname": nickname,
                "hinhThuc": "Nạp tiền" if tx_type == "DEPOSIT" else "Rút tiền",
                "transactionId": transaction_id,
                "amount": amount,
                "time": tx_time,
                "deviceNap": "",
            }
            
            # Gửi lưu (nếu 409 = đã tồn tại)
            save_resp = await asyncio.to_thread(
                lambda rec=record: requests.post(
                    f"{NODE_SERVER_URL}/api/transaction-details",
                    json=rec,
                    timeout=5
                )
            )
            
            if save_resp.status_code in (200, 201):
                saved.append(record)
            elif save_resp.status_code == 409:
                skipped += 1  # đã tồn tại
        
        if saved :
            label = "Nạp tiền" if tx_type == "DEPOSIT" else "Rút tiền"
            print(f"✅ [{username}] Lưu {len(saved)} giao dịch {label} mới (bỏ qua {skipped})")
        
        return saved
    
    except Exception as e:
        print(f"❌ [{username}] Lỗi fetch tx: {e}")
        return []


# Hàm sync wrapper (nếu cần gọi từ sync context)
def fetch_transactions(username: str, tx_type: str = "DEPOSIT", limit: int = 50):
    """
    Wrapper sync: chạy async function trong thread riêng (có event loop mới).
    """
    import asyncio
    
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(
                fetch_transactions_async(username, tx_type, limit)
            )
        finally:
            loop.close()
    
    return _run()


# ================= MAIN =================
if __name__ == "__main__":
    username = input("👉 Nhập username: ").strip()
    if not username:
        print("❌ Username không được để trống")
        exit()

    print(f"\n🔎 Đang lấy giao dịch NẠP cho {username}...")
    fetch_transactions(username, "DEPOSIT")

    print(f"\n🔎 Đang lấy giao dịch RÚT cho {username}...")
    fetch_transactions(username, "WITHDRAW")
