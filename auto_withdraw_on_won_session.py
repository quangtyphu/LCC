

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
QUEUE_STATE_FILE = "queue_state.json"

# Pending list: {username: {'amount': int, 'target_total_bet': int, 'added_at': timestamp}}
pending_withdrawals: Dict[str, Dict] = {}

# Required bet list: {username: target_total_bet}
required_bets: Dict[str, int] = {}

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


def _load_queue_state() -> dict:
    try:
        with open(QUEUE_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
            if not isinstance(data, dict):
                return {}
            # Normalize withdraw_queue
            wq = data.get("withdraw_queue")
            if isinstance(wq, list):
                migrated = {}
                for item in wq:
                    if isinstance(item, dict) and item.get("username"):
                        migrated[item["username"]] = item
                data["withdraw_queue"] = migrated
            elif not isinstance(wq, dict):
                data["withdraw_queue"] = {}
            # Normalize wager_queue
            gq = data.get("wager_queue")
            if isinstance(gq, list):
                migrated = {}
                for item in gq:
                    if isinstance(item, dict) and item.get("username"):
                        migrated[item["username"]] = item.get("target_total_bet")
                data["wager_queue"] = migrated
            elif not isinstance(gq, dict):
                data["wager_queue"] = {}
            return data
    except Exception:
        return {}


def _save_queue_state(state: dict) -> None:
    try:
        with open(QUEUE_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass


def _sync_state_to_file() -> None:
    state = _load_queue_state()
    state["withdraw_queue"] = pending_withdrawals
    state["wager_queue"] = required_bets
    _save_queue_state(state)


def _load_state_into_memory() -> None:
    state = _load_queue_state()
    wq = state.get("withdraw_queue")
    gq = state.get("wager_queue")
    if isinstance(wq, dict):
        pending_withdrawals.update(wq)
    if isinstance(gq, dict):
        required_bets.update(gq)



# ================= HELPER FUNCTIONS =================

def _is_pending_worker_enabled() -> bool:
    """
    Cho phép worker tự retry pending hay không.
    Mặc định tắt để chỉ rút khi có won-session.
    """
    config = load_config()
    try:
        return int(config.get("AUTO_WITHDRAW_PENDING_WORKER", 0)) == 1
    except Exception:
        return False


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
    Trả về nhóm của user: V2, V3, V1 (PRIORITY_USERS) hoặc DEFAULT
    """
    config = load_config()
    v2_users = [u for u in config.get("PRIORITY_USERS_V2", []) if u and u.strip()]
    v3_users = [u for u in config.get("PRIORITY_USERS_V3", []) if u and u.strip()]
    v1_users = [u for u in config.get("PRIORITY_USERS", []) if u and u.strip()]

    if username in v2_users:
        return "V2"
    if username in v3_users:
        return "V3"
    if username in v1_users:
        return "V1"
    return "DEFAULT"


def get_withdraw_threshold(group: str = "DEFAULT") -> int:
    """Lấy ngưỡng số dư tối thiểu để rút tiền theo group (VND)"""
    config = load_config()

    if group in ("V2", "V3"):
        v2_min = _get_v2_withdraw_min(config)
        if v2_min is not None:
            return v2_min
        return int(config.get("WITHDRAW_THRESHOLD_MIN_V1", config.get("WITHDRAW_THRESHOLD_MIN", 300000)))

    if group == "V1":
        return int(config.get("WITHDRAW_THRESHOLD_MIN_V1", config.get("WITHDRAW_THRESHOLD_MIN", 300000)))

    return int(config.get("WITHDRAW_THRESHOLD_MIN", 300000))


def _get_v2_withdraw_min(cfg: dict) -> Optional[int]:
    """
    Lấy min rút theo khung giờ V2_TIME_RULES (áp dụng cho V2/V3).
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
        match = re.search(r'vui lòng chơi thêm\s+(.+)$', error_message, re.IGNORECASE)
        if match:
            raw = match.group(1)
            # Giữ lại chữ số, loại bỏ dấu chấm/phẩy/khoảng trắng/NBSP
            digits = re.sub(r"[^\d]", "", raw)
            if digits:
                return int(digits)
    except Exception as e:
        print(f"⚠️ Lỗi parse error message: {e}")
    
    return None

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
    """
    Chỉ set cooldown 60s khi rút thành công (code 0/1).
    Code khác (vd: -10) không block lệnh rút acc tiếp theo.
    """
    global last_withdraw_at
    with global_withdraw_lock:
        if not _can_attempt_withdraw_now():
            return {"ok": False, "cooldown": True, "error": "Global cooldown active"}
    from withdraw import withdraw
    result = withdraw(username, amount)
    response_data = result.get("response", {})
    error_code = _parse_code(response_data.get("code"))
    if error_code in (0, 1):
        with global_withdraw_lock:
            last_withdraw_at = time.time()
    return result


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
    # Luôn bật worker khi có pending để đảm bảo retry rút
    _ensure_pending_worker_running()
    _sync_state_to_file()
    return amount


def _cancel_pending_withdrawal(username: str, reason: str = ""):
    if username in pending_withdrawals:
        del pending_withdrawals[username]
        _sync_state_to_file()
    if reason:
        print(f"⚠️ [AutoWithdraw][{username}] Hủy pending: {reason}")


def _clear_pending_silent(username: str):
    if username in pending_withdrawals:
        del pending_withdrawals[username]
        _sync_state_to_file()


def _set_required_bet(username: str, need_more: int):
    if need_more <= 0:
        return
    current_total = get_total_bet_for_user(username)
    required_bets[username] = current_total + need_more
    _sync_state_to_file()


def _clear_required_bet(username: str):
    if username in required_bets:
        del required_bets[username]
        _sync_state_to_file()


def _is_required_bet_met(username: str) -> bool:
    target = required_bets.get(username)
    if not target:
        return True
    current_total = get_total_bet_for_user(username)
    if current_total >= target:
        _clear_required_bet(username)
        return True
    return False


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

# Load persisted queues and start worker if needed
_load_state_into_memory()
if pending_withdrawals:
    _ensure_pending_worker_running()


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
        status = pending.get("status") or "ready"
        if status != "ready":
            _cancel_pending_withdrawal(username, f"invalid pending status: {status}")
            continue

        # Dùng user_lock để tránh race với handle_won_session_withdrawal (rút 2 lần)
        if username not in processing_users:
            processing_users[username] = threading.Lock()
        user_lock = processing_users[username]
        if not user_lock.acquire(blocking=False):
            # handle_won_session đang xử lý user này → skip, chờ vòng sau
            continue

        try:
            # Re-check: user có thể đã bị clear bởi handle_won_session
            if username not in pending_withdrawals:
                continue

            # Lấy balance mới nhất nếu có
            balance = _get_latest_balance(username)
            if balance is None:
                balance = pending.get("last_balance", 0)

            amount = pending.get("amount") or find_nearest_withdraw_amount(balance)
            if not amount:
                continue

            print(f"⏳ [AutoWithdraw][{username}] Pending -> Try rút {amount:,}đ")
            result = _withdraw_for_pending(username, amount)
        finally:
            user_lock.release()

        if result.get("cooldown"):
            return

        # Nếu bị [-10] thì lưu required_bets để chờ đủ cược
        response_data = result.get("response", {}) if isinstance(result, dict) else {}
        error_code = _parse_code(response_data.get("code"))
        full_message = response_data.get("message", "")
        if error_code == -10 and "chưa đủ điều kiện" in str(full_message).lower():
            need_more = parse_required_bet_from_error(full_message)
            if need_more:
                _set_required_bet(username, need_more)

        # Đã thực hiện lệnh rút => xóa khỏi pending để user chơi tiếp
        _clear_pending_silent(username)
        return

# ================= WITHDRAW LOGIC =================

def handle_won_session_withdrawal(username: str, balance: int) -> dict:

    user_group = get_user_group(username)
    threshold = get_withdraw_threshold(user_group)

    # 0. Nếu đang còn yêu cầu cược (wager_queue) thì không rút
    if not _is_required_bet_met(username):
        target_total = required_bets.get(username)
        return {
            "ok": True,
            "withdrew": False,
            "message": f"Chưa đủ cược (target_total_bet={target_total}), skip withdraw"
        }
    
    # 1. Check balance threshold
    if balance <= threshold:
        return {
            "ok": True,
            "withdrew": False,
            "message": f"Balance {balance:,} <= threshold {threshold:,} (group {user_group}), skip"
        }
    
    # 2. Nếu đã có pending -> chỉ chờ cooldown (không chặn theo cược)
    if username in pending_withdrawals:
        pending = pending_withdrawals[username]

    # 4. Calculate withdraw amount
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
    
    # 5. CHECK: User có trong pending list không?
    if username in pending_withdrawals:
        pending = pending_withdrawals[username]
        status = pending.get("status") or "ready"
        if status != "ready":
            _cancel_pending_withdrawal(username, f"invalid pending status: {status}")
            return {
                "ok": True,
                "withdrew": False,
                "message": "Pending status không hợp lệ, đã hủy"
            }

        amount = pending.get("amount") or find_nearest_withdraw_amount(balance)
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
        # Đã thực hiện lệnh rút => xóa khỏi pending để user chơi tiếp
        _clear_pending_silent(username)
        response_data = result.get("response", {})
        error_code = _parse_code(response_data.get("code"))
        full_message = response_data.get("message", "")
        if result.get("ok") or error_code in (0, 1):
            return {
                "ok": True,
                "withdrew": True,
                "amount": amount,
                "message": "Rút tiền thành công"
            }
        if error_code == -14:
            _cancel_pending_withdrawal(username, "[-14] Số dư không đủ")
            return {
                "ok": False,
                "withdrew": False,
                "error": "[-14] Số dư không đủ, hủy pending"
            }
        if error_code == -10 and "chưa đủ điều kiện" in str(full_message).lower():
            # Lưu yêu cầu cược vào kho riêng, không giữ pending
            need_more = parse_required_bet_from_error(full_message)
            if need_more:
                _set_required_bet(username, need_more)
            _clear_pending_silent(username)
            return {
                "ok": False,
                "withdrew": False,
                "error": "[-10] Chưa đủ điều kiện rút, chờ đủ cược"
            }

        # Lỗi khác: luôn nhả khỏi pending để user tiếp tục chơi
        _cancel_pending_withdrawal(
            username,
            f"Code {error_code}" if error_code is not None else "Rút tiền thất bại"
        )
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
            need_more = parse_required_bet_from_error(full_message)
            if need_more:
                _set_required_bet(username, need_more)
            return {
                "ok": False,
                "withdrew": False,
                "error": "[-10] Chưa đủ điều kiện rút, chờ đủ cược"
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


