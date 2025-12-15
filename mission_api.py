"""
Mission API - Quản lý nhiệm vụ hàng ngày
"""
import sys
import io

# Fix encoding cho Windows console
if sys.platform == 'win32':
    import os
    os.system('chcp 65001 > nul')

import time
from datetime import datetime
from game_api_helper import game_request_with_retry, update_user_balance

MISSION_URL = "https://wlb.tele68.com/v1/mission"


def fetch_missions(username: str, mission_type: str = "daily") -> dict:
    """
    Lấy danh sách nhiệm vụ.
    
    Args:
        username: Username trong DB
        mission_type: Loại nhiệm vụ (daily, weekly, monthly...)
    
    Returns:
        {
            "ok": True,
            "data": [...],
            "total": 18
        }
    """
    # Params cho GET
    params = {"type": mission_type}
    
    # Gọi API qua helper
    resp = game_request_with_retry(username, "GET", MISSION_URL, params=params)
    
    if not resp:
        print(f"❌ [{username}] Không gọi được API mission")
        return {"ok": False, "error": "Không gọi được API mission"}
    
    if not resp.ok:
        print(f"❌ [{username}] HTTP {resp.status_code}: {resp.text[:200]}")
        return {"ok": False, "error": f"HTTP {resp.status_code}"}
    
    try:
        data = resp.json()
        
        if isinstance(data, list):
            return {
                "ok": True,
                "data": data,
                "total": len(data)
            }
        else:
            print(f"❌ [{username}] Response format không hợp lệ: {data}")
            return {"ok": False, "error": "Response không phải list"}
    
    except Exception as e:
        print(f"❌ [{username}] Lỗi parse response: {e}")
        return {"ok": False, "error": f"Lỗi parse: {e}"}


def claim_mission(username: str, mission_name: str, event_date: str, mission_type: str = "daily", prize_amount: int = 0) -> dict:
    """
    Nhận thưởng nhiệm vụ đã hoàn thành.
    
    Args:
        username: Username trong DB
        mission_name: Tên nhiệm vụ (vd: "MISSION_DAILY_RECHARGE_BY_BANK")
        event_date: Ngày sự kiện (vd: "2025-12-15")
        mission_type: Loại nhiệm vụ (daily, weekly...)
        prize_amount: Số tiền thưởng (để log)
    
    Returns:
        {
            "ok": True,
            "balance": 324600,  # Số dư mới sau khi nhận thưởng
            "prizeAmount": 10000,  # Số tiền thưởng nhận được
            "prizeDiamond": 0,
            "message": "..."
        }
    """
    payload = {
        "eventDate": event_date,
        "name": mission_name,
        "type": mission_type
    }
    
    # Gọi API PUT qua helper
    resp = game_request_with_retry(username, "PUT", MISSION_URL, json_data=payload)
    
    if not resp:
        print(f"❌ [{username}] Không gọi được API claim mission")
        return {"ok": False, "error": "Không gọi được API claim mission"}
    
    if not resp.ok:
        print(f"❌ [{username}] HTTP {resp.status_code}: {resp.text[:200]}")
        return {"ok": False, "error": f"HTTP {resp.status_code}"}
    
    try:
        data = resp.json()
        # Response: {"prizeDiamondAmount": 0, "balance": 324600}
        # Note: "balance" là số dư mới sau khi nhận thưởng
        
        new_balance = data.get("balance", 0)  # Số dư mới
        prize_diamond = data.get("prizeDiamondAmount", 0)
        
        if new_balance > 0 or prize_diamond > 0:
            # Log: Nhận thưởng X → Số dư Y
            print(f"✅ [{username}] Nhận thưởng Nhiệm Vụ: +{prize_amount:,}đ → Số dư: {new_balance:,}đ")
            
            # Cập nhật balance vào DB
            update_user_balance(username, float(new_balance))
            
            return {
                "ok": True,
                "balance": new_balance,  # Số dư mới
                "prizeAmount": prize_amount,  # Số tiền thưởng
                "prizeDiamond": prize_diamond,
                "message": "Nhận thưởng thành công"
            }
        else:
            print(f"⚠️ [{username}] Nhận thưởng nhưng không có balance: {data}")
            return {
                "ok": False,
                "error": "Không có balance trong response",
                "response": data
            }
    
    except Exception as e:
        print(f"❌ [{username}] Lỗi parse response: {e}")
        return {"ok": False, "error": f"Lỗi parse: {e}"}


def auto_claim_missions(username: str, mission_type: str = "daily"):
    """
    Tự động nhận tất cả nhiệm vụ đã hoàn thành (isWon=true, claimedAt=null).
    
    Args:
        username: Username trong DB
        mission_type: Loại nhiệm vụ (daily, weekly...)
    """
    # 1. Lấy danh sách nhiệm vụ
    result = fetch_missions(username, mission_type)
    
    if not result.get("ok"):
        return
    
    missions = result.get("data", [])
    
    # 2. Lọc nhiệm vụ đã hoàn thành nhưng chưa nhận
    # isWon=true AND claimedAt=null
    unclaimed = [
        m for m in missions 
        if m.get("isWon") and not m.get("claimedAt")
    ]
    
    if not unclaimed:
        return
    
    # 3. Nhận từng nhiệm vụ
    for i, mission in enumerate(unclaimed, 1):
        name = mission.get("name")
        event_date = mission.get("eventDate")
        prize = mission.get("prizeVinAmount", 0)
        
        # Nhận thưởng (truyền prize_amount để log)
        claim_result = claim_mission(username, name, event_date, mission_type, prize)
        
        # Delay 2s giữa các lần claim
        if i < len(unclaimed):
            time.sleep(2)


if __name__ == "__main__":
    """
    Chạy trực tiếp file này để test:
    python mission_api.py
    
    Hoặc từ file khác:
    from mission_api import auto_claim_missions
    auto_claim_missions("username")
    """
    username = input("Nhập username: ").strip()
    if username:
        print(f"\n🔍 Đang kiểm tra nhiệm vụ cho [{username}]...\n")
        auto_claim_missions(username)
        print(f"\n✅ Hoàn tất kiểm tra nhiệm vụ cho [{username}]")
    else:
        print("❌ Username không được để trống!")
