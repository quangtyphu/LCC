

import sys
import os
import requests
import time
import json
import re
import asyncio
from typing import Dict, Optional, Tuple
from zoneinfo import ZoneInfo

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

# Global cooldown between withdraw commands (all users)
WITHDRAW_GLOBAL_COOLDOWN_SECONDS = 60  # 1 phút
last_withdraw_at: float = 0.0

# Lock để tránh race condition khi xử lý withdraw của cùng 1 user
import threading
processing_users: Dict[str, threading.Lock] = {}
global_withdraw_lock = threading.Lock()
pending_worker_thread: Optional[threading.Thread] = None

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
        if group == "V2":
            v2_min = _get_v2_withdraw_min(config)
            if v2_min is not None:
                return v2_min
        return int(config.get("WITHDRAW_THRESHOLD_MIN_V1", config.get("WITHDRAW_THRESHOLD_MIN", 300000)))

    return int(config.get("WITHDRAW_THRESHOLD_MIN", 300000))


def _get_v2_withdraw_min(cfg: dict) -> Optional[int]:
    """
    Lấy min rút riêng cho V2 theo khung giờ (nếu có).
    """
    from datetime import datetime as dt
    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    now = dt.now(tz).time()
    windows = cfg.get("V2_TIME_RULES") or []
    for w in windows:
        try:
            if int(w.get("enabled", 1)) != 1:
                continue
            start = w.get("start")
            end = w.get("end")
            min_withdraw = w.get("withdraw_min")
            if min_withdraw is None or not start or not end:
                continue
            s = dt.strptime(start, "%H:%M").time()
            e = dt.strptime(end, "%H:%M").time()
            in_range = (s <= now < e) if s < e else (now >= s or now < e)
            if in_range:
                return int(min_withdraw)
        except Exception:
            continue
    return None


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


def is_user_waiting_to_withdraw(username: str) -> bool:
    pending = pending_withdrawals.get(username) or {}
    return pending.get("status") == "ready"


def _can_attempt_withdraw_now() -> bool:
    return (time.time() - last_withdraw_at) >= WITHDRAW_GLOBAL_COOLDOWN_SECONDS


def _withdraw_with_global_cooldown(username: str, amount: int) -> dict:
    global last_withdraw_at
    with global_withdraw_lock:
        if not _can_attempt_withdraw_now():
            return {"ok": False, "cooldown": True, "error": "Global cooldown active"}
        # Ghi nhận thời điểm gửi lệnh rút (dù thành công hay thất bại)
        last_withdraw_at = time.time()
    from withdraw import withdraw
    return withdraw(username, amount)


def _withdraw_for_pending(username: str, amount: int) -> dict:
    """
    Rút tiền trong pending queue:
    - Chỉ set cooldown khi code 0/1
    - Code khác sẽ không block lệnh tiếp theo
    """
    global last_withdraw_at
    with global_withdraw_lock:
        if not _can_attempt_withdraw_now():
            return {"ok": False, "cooldown": True, "error": "Global cooldown active"}
    from withdraw import withdraw
    result = withdraw(username, amount)
    response_data = result.get("response", {})
    error_code = response_data.get("code")
    if error_code in (0, 1):
        with global_withdraw_lock:
            last_withdraw_at = time.time()
    return result


def _get_latest_balance(username: str) -> Optional[int]:
    try:
        from get_balance import get_balance as get_balance_func
        result = get_balance_func(username)
        if isinstance(result, dict) and result.get("ok"):
            return int(result.get("balance") or 0)
    except Exception:
        return None
    return None


def _mark_pending_ready(username: str, balance: int, reason: str = "balance_ready") -> Optional[int]:
    amount = find_nearest_withdraw_amount(balance)
    if not amount:
        return None
    pending_withdrawals[username] = {
        "amount": amount,
        "target_total_bet": None,
        "added_at": time.time(),
        "status": "ready",
        "reason": reason,
        "last_balance": balance,
    }
    _ensure_pending_worker_running()
    return amount


def _cancel_pending_withdrawal(username: str, reason: str = ""):
    if username in pending_withdrawals:
        del pending_withdrawals[username]
    if reason:
        print(f"⚠️ [AutoWithdraw][{username}] Hủy pending: {reason}")


def _ensure_pending_worker_running():
    global pending_worker_thread
    if pending_worker_thread and pending_worker_thread.is_alive():
        return

    def _worker_loop():
        while True:
            try:
                _process_pending_withdrawals()
            except Exception as e:
                print(f"⚠️ [AutoWithdraw] Pending worker error: {e}")
            time.sleep(5)

    pending_worker_thread = threading.Thread(target=_worker_loop, daemon=True)
    pending_worker_thread.start()


def _process_pending_withdrawals():
    if not pending_withdrawals:
        return
    if not _can_attempt_withdraw_now():
        return

    # FIFO theo added_at
    candidates = sorted(
        pending_withdrawals.items(),
        key=lambda kv: kv[1].get("added_at", 0)
    )

    for username, pending in candidates:
        status = pending.get("status") or ("need_bet" if pending.get("target_total_bet") else "ready")
        if status == "need_bet":
            target_bet = pending.get("target_total_bet")
            if not target_bet:
                continue
            if not if_user_reached_bet_target(username, target_bet):
                continue

        # Lấy balance mới nhất nếu có
        balance = _get_latest_balance(username)
        if balance is None:
            balance = pending.get("last_balance", 0)

        amount = find_nearest_withdraw_amount(balance) or pending.get("amount")
        if not amount:
            continue

        print(f"⏳ [AutoWithdraw][{username}] Pending -> Try rút {amount:,}đ")
        result = _withdraw_for_pending(username, amount)

        if result.get("cooldown"):
            return

        if result.get("ok"):
            del pending_withdrawals[username]
            return

        # Nếu lỗi -10 thì cập nhật pending sang cần cược thêm
        response_data = result.get("response", {})
        error_code = response_data.get("code")
        full_message = response_data.get("message", "")

        if error_code == -14:
            _cancel_pending_withdrawal(username, "[-14] Số dư không đủ")
            continue

        if error_code == -10 and "chưa đủ điều kiện" in str(full_message).lower():
            required_additional = parse_required_bet_from_error(full_message)
            if required_additional and required_additional > 0:
                current_total = get_total_bet_for_user(username)
                target_total_bet = current_total + required_additional
                pending_withdrawals[username] = {
                    "amount": amount,
                    "target_total_bet": target_total_bet,
                    "added_at": time.time(),
                    "status": "need_bet",
                    "reason": "need_bet_after_-10",
                    "last_balance": balance,
                }
            continue

        # Lỗi khác: giữ pending để thử lại sau
        _cancel_pending_withdrawal(username, f"Code {error_code}")
        continue

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

    # Đủ điều kiện rút => đưa vào danh sách đợi rút (để dừng cược)
    if username not in pending_withdrawals:
        _mark_pending_ready(username, balance, reason="balance_ready")
    
    # 4. CHECK: User có trong pending list không?
    if username in pending_withdrawals:
        pending = pending_withdrawals[username]
        status = pending.get("status") or ("need_bet" if pending.get("target_total_bet") else "ready")

        if status == "need_bet":
            target_bet = pending.get("target_total_bet")
            if not target_bet:
                return {
                    "ok": False,
                    "withdrew": False,
                    "error": "Pending list thiếu target_total_bet"
                }

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
                    result = _withdraw_with_global_cooldown(username, amount)
                    if result.get("cooldown"):
                        _mark_pending_ready(username, balance, reason="cooldown_wait")
                        return {
                            "ok": True,
                            "withdrew": False,
                            "pending": True,
                            "message": "Global cooldown active, added to pending list"
                        }
                    
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
                        response_data = result.get("response", {})
                        error_code = response_data.get("code")
                        if error_code == -14:
                            _cancel_pending_withdrawal(username, "[-14] Số dư không đủ")
                            return {
                                "ok": False,
                                "withdrew": False,
                                "error": "[-14] Số dư không đủ, hủy pending"
                            }
                        
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

        # status == "ready"
        amount = find_nearest_withdraw_amount(balance) or pending.get("amount")
        if not amount:
            return {
                "ok": False,
                "withdrew": False,
                "error": f"Balance {balance:,} quá cao hoặc quá thấp"
            }
        result = _withdraw_with_global_cooldown(username, amount)
        if result.get("cooldown"):
            _mark_pending_ready(username, balance, reason="cooldown_wait")
            return {
                "ok": True,
                "withdrew": False,
                "pending": True,
                "message": "Global cooldown active, added to pending list"
            }
        if result.get("ok"):
            del pending_withdrawals[username]
            return {
                "ok": True,
                "withdrew": True,
                "amount": amount,
                "message": "Rút tiền thành công"
            }
        response_data = result.get("response", {})
        error_code = response_data.get("code")
        full_message = response_data.get("message", "")
        if error_code == -14:
            _cancel_pending_withdrawal(username, "[-14] Số dư không đủ")
            return {
                "ok": False,
                "withdrew": False,
                "error": "[-14] Số dư không đủ, hủy pending"
            }
        if error_code == -10 and "chưa đủ điều kiện" in str(full_message).lower():
            required_additional = parse_required_bet_from_error(full_message)
            if required_additional and required_additional > 0:
                current_total = get_total_bet_for_user(username)
                target_total_bet = current_total + required_additional
                pending_withdrawals[username] = {
                    "amount": amount,
                    "target_total_bet": target_total_bet,
                    "added_at": time.time(),
                    "status": "need_bet",
                    "reason": "need_bet_after_-10",
                    "last_balance": balance,
                }
        return {
            "ok": False,
            "withdrew": False,
            "error": result.get("error") or "Rút tiền thất bại"
        }
    
    # 5. User KHÔNG có trong list → Try rút lần đầu
    print(f"💰 [{username}] Try rút {withdraw_amount:,}đ (balance: {balance:,})")
    
    try:
        result = _withdraw_with_global_cooldown(username, withdraw_amount)
        if result.get("cooldown"):
            _mark_pending_ready(username, balance, reason="cooldown_wait")
            return {
                "ok": True,
                "withdrew": False,
                "pending": True,
                "message": "Global cooldown active, added to pending list"
            }
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
                    "added_at": time.time(),
                    "status": "need_bet",
                    "reason": "need_bet_after_-10",
                    "last_balance": balance,
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
        elif error_code == -14:
            return {
                "ok": False,
                "withdrew": False,
                "error": "[-14] Số dư không đủ, hủy rút"
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


