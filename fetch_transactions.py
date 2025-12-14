import requests
from datetime import datetime

# API server Node của bạn (CMS local)
NODE_SERVER_URL = "http://127.0.0.1:3000"   # đổi thành IP nếu cần
HISTORY_URL = "https://wsslot.tele68.com/v1/lobby/transaction/history"


def fetch_transactions(username: str, tx_type: str = "DEPOSIT", limit: int = 50):
    """
    Lấy giao dịch từ API gốc (tele68) qua proxy, rồi lưu vào CMS server (Mongo).
    """
    # 1️⃣ Lấy thông tin user từ CMS server
    try:
        r = requests.get(f"{NODE_SERVER_URL}/api/users/{username}", timeout=5)
        if r.status_code != 200:
            print(f"❌ Không tìm thấy user {username} (API trả {r.status_code})")
            return []
        acc = r.json()
    except Exception as e:
        print(f"❌ Lỗi gọi API lấy user {username}: {e}")
        return []

    jwt = acc.get("jwt")
    access_token = acc.get("accessToken")
    proxy_str = acc.get("proxy")
    nickname = acc.get("nickname", "")

    if not jwt or not access_token or not proxy_str:
        print(f"⚠️ {username} thiếu jwt / accessToken / proxy")
        return []

    # 2️⃣ Proxy setup
    try:
        host, port, userp, passp = proxy_str.split(":")
        proxy_auth = f"{userp}:{passp}@{host}:{port}"
        proxy_url = f"socks5h://{proxy_auth}"
        proxies = {"http": proxy_url, "https": proxy_url}
    except Exception as e:
        print(f"⚠️ Proxy sai định dạng ({proxy_str}): {e}")
        return []

    # 3️⃣ Call API lịch sử giao dịch từ tele68
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

    try:
        r = requests.get(HISTORY_URL, params=params, headers=headers, proxies=proxies, timeout=20)
        if not r.ok:
            print(f"❌ API lịch sử lỗi {r.status_code}: {r.text}")
            return []
        data = r.json()
    except Exception as e:
        print(f"❌ Lỗi fetch transactions cho {username}: {e}")
        return []

    saved = []
    skipped = 0

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

        # 4️⃣ Gửi về CMS server (Mongo sẽ lưu vào collection transaction_details)
        try:
            resp = requests.post(f"{NODE_SERVER_URL}/api/transaction-details", json=record, timeout=5)
            if resp.status_code in (200, 201):
                saved.append(record)
            elif resp.status_code == 409:
                skipped += 1  # đã tồn tại
            else:
                print(f"⚠️ [{username}] Lỗi lưu transaction {transaction_id}: {resp.text}")
        except Exception as e:
            print(f"⚠️ [{username}] Không lưu được transaction {transaction_id}: {e}")

    if saved:
        label = "Nạp tiền" if tx_type == "DEPOSIT" else "Rút tiền"
        print(f"✅ [{username}] Lưu {len(saved)} giao dịch {label} mới (bỏ qua {skipped})")

    return saved


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
