import sys
import io

# Fix encoding cho Windows console
if sys.platform == 'win32':
    import os
    os.system('chcp 65001 > nul')

import os
import time
from datetime import datetime

import requests

from game_api_helper import game_request_with_retry, update_user_balance

GIFT_BOX_URL = "https://wlb.tele68.com/v1/lobby/gift-box"
CLAIM_GIFT_URL = "https://wlb.tele68.com/v1/lobby/gift-box/item"

# CMS lưu lịch sử nhận hòm quà (server.js → SQLite game_data.db)
CMS_GIFT_BOX_CLAIMS_URL = os.environ.get(
    "CMS_GIFT_BOX_CLAIMS_URL",
    "http://127.0.0.1:3000/api/gift-box-claims",
)


def _record_gift_claim_to_cms(log_line: str) -> None:
    received_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        r = requests.post(
            CMS_GIFT_BOX_CLAIMS_URL,
            json={"line": log_line.strip(), "received_at": received_at},
            timeout=8,
        )
        if not r.ok:
            print(
                f"⚠️ CMS gift-box-claims HTTP {r.status_code}: {r.text[:300]}",
                flush=True,
            )
    except requests.RequestException as e:
        print(f"⚠️ Không gọi được CMS gift-box-claims: {e}", flush=True)

def fetch_gift_box(username: str) -> dict:
    
    # Gọi API qua helper
    resp = game_request_with_retry(username, "GET", GIFT_BOX_URL)
    
    if not resp:
        return {"ok": False, "error": "Không gọi được API gift-box"}
    
    result = {"ok": resp.ok, "status": resp.status_code}
    try:
        result["data"] = resp.json()
    except Exception:
        result["text"] = resp.text
    return result

def claim_gift(username: str, gift_id: str) -> dict:
    
    payload = {"id": gift_id}
    
    # Gọi API qua helper
    resp = game_request_with_retry(username, "POST", CLAIM_GIFT_URL, json_data=payload)
    
    if not resp:
        return {"ok": False, "error": "Không gọi được API claim gift"}
    
    result = {"ok": resp.ok, "status": resp.status_code}
    try:
        result["data"] = resp.json()
    except Exception:
        result["text"] = resp.text
    return result

def auto_claim_gifts(username: str):
    """
    Check danh sách quà, nếu có quà chưa nhận → nhận từng cái, cách nhau 3s.
    Chỉ log khi có quà được nhận thành công.
    """
    result = fetch_gift_box(username)

    if not result.get("ok"):
        return

    data = result.get("data")
    if not isinstance(data, list) or not data:
        return

    # Lọc quà chưa nhận
    unclaimed = [g for g in data if not g.get("isClaim", False)]
    if not unclaimed:
        return

    # Nhận từng quà
    for i, g in enumerate(unclaimed, 1):
        gift_id = g.get("id", "")
        title = g.get("title", "")
        amount = g.get("bonusAmount", 0)
        created = g.get("createTime", "")
        
        claim_result = claim_gift(username, gift_id)
        if claim_result.get("ok"):
            claim_data = claim_result.get("data", {})
            
            if isinstance(claim_data, dict):
                balance = claim_data.get("balance")
                
                # Chỉ log khi nhận thành công
                if balance is not None:
                    log_line = f"🎁 [{username}] {created} | Nhận: {title} (+{amount:,}đ) → Số dư: {balance:,}đ"
                    print(log_line)
                    _record_gift_claim_to_cms(log_line)
                    update_user_balance(username, float(balance))

        # Delay 2s giữa các lần claim
        if i < len(unclaimed):
            time.sleep(2)

if __name__ == "__main__":
    """
    Chạy trực tiếp file này để test:
    python gift_box_api.py
    """
    username = input("Nhập username: ").strip()
    if username:
        print(f"\n🔍 Đang kiểm tra quà cho [{username}]...\n")
        auto_claim_gifts(username)
        print(f"\n✅ Hoàn tất kiểm tra quà cho [{username}]")
    else:
        print("❌ Username không được để trống!")