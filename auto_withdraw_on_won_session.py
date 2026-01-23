

import sys
import os
import requests
import time
import json
import re
import asyncio
from typing import Dict, Optional, Tuple

# Fix encoding cho Windows console
if sys.platform == 'win32':
    os.system('chcp 65001 > nul')

# ================= CONSTANTS =================
API_BASE = "http://127.0.0.1:3000"
WITHDRAW_AMOUNTS = [300000, 500000, 600000, 800000, 1000000, 2000000]  # VND

# Pending list: {username: {'amount': int, 'target_total_bet': int, 'added_at': timestamp}}
pending_withdrawals: Dict[str, Dict] = {}

# Success cooldown: {username: next_attempt_time}
success_cooldowns: Dict[str, float] = {}
SUCCESS_COOLDOWN_SECONDS = 300  # 5 phút (300 giây)

# Lock để tránh race condition khi xử lý withdraw của cùng 1 user
import threading
processing_users: Dict[str, threading.Lock] = {}

# ================= HELPER FUNCTIONS =================

def load_config() -> dict:
    """Lấy cấu hình từ config.json"""
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            config = json.load(f)
            return config
    except Exception as e:
        print(f"⚠️ Lỗi đọc config.json: {e}")
        return {}


def get_user_group(username: str) -> str:
    """
    Trả về nhóm của user: V2, V3 hoặc DEFAULT
    """
    config = load_config()
    v2_users = [u for u in config.get("PRIORITY_USERS_V2", []) if u and u.strip()]
    v3_users = [u for u in config.get("PRIORITY_USERS_V3", []) if u and u.strip()]

    if username in v2_users:
        return "V2"
    if username in v3_users:
        return "V3"
    return "DEFAULT"


def get_withdraw_threshold(group: str = "DEFAULT") -> int:
    """Lấy ngưỡng số dư tối thiểu để rút tiền theo group (VND)"""
    config = load_config()

    if group in ("V2", "V3"):
        return int(config.get("WITHDRAW_THRESHOLD_MIN_V1", config.get("WITHDRAW_THRESHOLD_MIN", 300000)))

    return int(config.get("WITHDRAW_THRESHOLD_MIN", 300000))


def find_nearest_withdraw_amount(balance: int) -> Optional[int]:

    if balance < WITHDRAW_AMOUNTS[0]:
        return None
    
    # Tìm số tiền lớn nhất ≤ balance
    for amount in reversed(WITHDRAW_AMOUNTS):
        if balance >= amount:
            return amount
    
    return None


def get_total_bet_for_user(username: str) -> int:

    try:
        r = requests.get(
            f"{API_BASE}/api/bet-totals",
            params={"username": username},
            timeout=5
        )
        
        if r.status_code != 200:
            print(f"⚠️ [{username}] API bet-totals error: {r.status_code}")
            return 0
        
        data = r.json()
        
        # API trả về direct object nếu có username param
        if isinstance(data, dict):
            # Dùng total_all (tổng cược tổng) vì không bị reset qua ngày/tuần/tháng
            total = int(data.get("total_all") or 0)
            return total
        
        return 0
        
    except Exception as e:
        print(f"⚠️ [{username}] Lỗi lấy tổng cược: {e}")
        return 0


def parse_required_bet_from_error(error_message: str) -> Optional[int]:

    try:
        # Tìm pattern "vui lòng chơi thêm XXX"
        import re
        # Tìm số tiền (có thể có dấu chấm phân cách)
        match = re.search(r'vui lòng chơi thêm\s+([\d.]+)', error_message, re.IGNORECASE)
        if match:
            amount_str = match.group(1).replace(".", "")
            return int(amount_str)
    except Exception as e:
        print(f"⚠️ Lỗi parse error message: {e}")
    
    return None


def extract_error_code_and_message(response_text: str) -> Tuple[Optional[int], str]:
    """
    Parse error code và message từ response của game API.
    
    Returns:
        (error_code, error_message)
    """
    try:
        data = json.loads(response_text) if isinstance(response_text, str) else response_text
        code = data.get("code")
        message = data.get("message", "")
        return code, message
    except Exception:
        return None, response_text


def if_user_reached_bet_target(username: str, target_total_bet: int) -> bool:

    current_total = get_total_bet_for_user(username)
    
    if current_total >= target_total_bet:
        return True
    else:
        return False

# ================= WITHDRAW LOGIC =================

def handle_won_session_withdrawal(username: str, balance: int) -> dict:

    user_group = get_user_group(username)
    threshold = get_withdraw_threshold(user_group)
    
    # 1. Check balance threshold
    if balance <= threshold:
        return {
            "ok": True,
            "withdrew": False,
            "message": f"Balance {balance:,} <= threshold {threshold:,} (group {user_group}), skip"
        }
    
    # 2. Calculate withdraw amount
    withdraw_amount = find_nearest_withdraw_amount(balance)
    if not withdraw_amount:
        return {
            "ok": True,
            "withdrew": False,
            "message": f"Balance {balance:,} too high (> 2M)"
        }
    
    # 4. CHECK: User có trong pending list không?
    if username in pending_withdrawals:
        pending = pending_withdrawals[username]
        target_bet = pending["target_total_bet"]   
      
        # Check xem đã đủ cược chưa
        if if_user_reached_bet_target(username, target_bet):
            # Đủ rồi → Tính lại amount từ balance MỚI
            amount = find_nearest_withdraw_amount(balance)
            if not amount:
                return {
                    "ok": False,
                    "withdrew": False,
                    "error": f"Balance {balance:,} quá cao hoặc quá thấp"
                }
            
            try:
                from withdraw import withdraw
                result = withdraw(username, amount)
                
                if result.get("ok"):
                    # Success!
                    
                    # Remove from pending
                    del pending_withdrawals[username]
                    
                    return {
                        "ok": True,
                        "withdrew": True,
                        "amount": amount,
                        "message": "Rút tiền thành công sau khi đủ cược"
                    }
                else:
                    # Failed again
                    print(f"❌ [{username}] Rút tiền vẫn thất bại: {result.get('error')}")
                    
                    # Keep in pending list, user cần cược thêm nữa
                    return {
                        "ok": False,
                        "withdrew": False,
                        "error": f"Rút lại thất bại: {result.get('error')}"
                    }
            except Exception as e:
                print(f"❌ [{username}] Exception: {e}")
                return {
                    "ok": False,
                    "withdrew": False,
                    "error": str(e)
                }
        else:
            # Chưa đủ → skip
            return {
                "ok": True,
                "withdrew": False,
                "message": "Chưa đủ cược, chờ won-session tiếp theo"
            }
    
    # 5. User KHÔNG có trong list → Try rút lần đầu
    print(f"💰 [{username}] Try rút {withdraw_amount:,}đ (balance: {balance:,})")
    
    try:
        from withdraw import withdraw
        result = withdraw(username, withdraw_amount)
    except Exception as e:
        return {
            "ok": False,
            "withdrew": False,
            "error": f"Exception when calling withdraw: {e}"
        }
    
    if result.get("ok"):
        # Success!
        return {
            "ok": True,
            "withdrew": True,
            "amount": withdraw_amount,
            "message": "Rút tiền thành công"
        }
    else:
        # Failed - check [-10]
        error_msg = result.get("error", "")
        response_data = result.get("response", {})
        error_code = response_data.get("code")
        full_message = response_data.get("message", "")
        
        print(f"❌ [{username}] Rút tiền thất bại: {error_msg}")
        
        # Check lỗi [-10]
        if error_code == -10 and "chưa đủ điều kiện" in full_message.lower():
            # Parse required additional bet
            required_additional = parse_required_bet_from_error(full_message)
            
            if required_additional and required_additional > 0:
                # Get current total bet
                current_total = get_total_bet_for_user(username)
                
                # Calculate target
                target_total_bet = current_total + required_additional
                
                print(f"📊 [{username}] Lỗi [-10]: Cần cược thêm {required_additional:,}đ")
                
                # ADD to pending list
                pending_withdrawals[username] = {
                    "amount": withdraw_amount,
                    "target_total_bet": target_total_bet,
                    "added_at": time.time()
                }
                
                return {
                    "ok": True,
                    "withdrew": False,
                    "message": f"Added to pending list (need {required_additional:,} more)",
                    "pending": True
                }
            else:
                print(f"⚠️ [{username}] Không parse được required bet từ message")
                return {
                    "ok": False,
                    "withdrew": False,
                    "error": "Cannot parse required bet from [-10] error"
                }
        else:
            # Other error
            return {
                "ok": False,
                "withdrew": False,
                "error": result.get("message", "Unknown error")
            }


# ================= ENTRY POINT (called from ws_events.py) =================

def handle_won_session_auto_withdraw(username: str, balance: int):
    """
    Entry point từ ws_events.py.
    QUAN TRỌNG: Chạy trong thread riêng để không block event loop async
    """
    # Tạo/lấy lock cho user này để tránh race condition
    if username not in processing_users:
        processing_users[username] = threading.Lock()
    
    user_lock = processing_users[username]
    
    # Chạy trong thread để không block websocket event loop
    def _run_in_thread():
        # Acquire lock để đảm bảo chỉ xử lý 1 request cho user này một lúc
        with user_lock:
            try:
                result = handle_won_session_withdrawal(username, balance)
                
                if result.get("pending"):
                    pass
                elif not result.get("ok"):
                    print(f"⚠️ [AutoWithdraw][{username}] Error: {result.get('error')}")
                    
            except Exception as e:
                print(f"❌ [AutoWithdraw][{username}] Exception: {e}")
                import traceback
                traceback.print_exc()
    
    # Tạo thread daemon để chạy
    thread = threading.Thread(target=_run_in_thread, daemon=True)
    thread.start()


# ================= TEST SCRIPT =================


