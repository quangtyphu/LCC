

import sys
import os
import requests
import time
import json
import re
import asyncio
from datetime import datetime
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

# Fix encoding cho Windows console
if sys.platform == 'win32':
    os.system('chcp 65001 > nul')

# ================= CONSTANTS =================
API_BASE = "http://127.0.0.1:3000"
WITHDRAW_AMOUNTS = [200000, 300000, 500000, 1000000, 2000000]  # VND
import os as _os

_QUEUE_DIR = _os.path.dirname(_os.path.abspath(__file__))
QUEUE_STATE_FILE = _os.path.join(_QUEUE_DIR, "queue_state.json")

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

# --- Khung 23:55–hết ngày: tối đa 1 user rút / phiên (chọn balance cao nhất trong pool > min) ---
_late_night_lock = threading.Lock()
_late_night_buffer: Dict[str, Dict[str, int]] = {}
_late_night_timers: Dict[str, threading.Timer] = {}
_late_night_consumed: set[str] = set()
_LATE_NIGHT_CONSUMED_MAX = 4000
_LATE_NIGHT_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_LATE_NIGHT_DEBOUNCE_SEC = 2.0


def _late_night_settings(config: dict) -> dict:
    """Đọc cụm LATE_NIGHT_AUTO_WITHDRAW; vẫn nhận key phẳng cũ nếu chưa có cụm."""
    defaults = {
        "ENABLED": 0,
        "START": "23:55",
        "END": "24:00",
        "MIN_BALANCE": 0,
    }
    out = dict(defaults)
    blk = config.get("LATE_NIGHT_AUTO_WITHDRAW")
    if isinstance(blk, dict):
        for k in defaults:
            if k in blk:
                out[k] = blk[k]
        return out
    legacy = {
        "ENABLED": "LATE_NIGHT_AUTO_WITHDRAW_ENABLED",
        "START": "LATE_NIGHT_AUTO_WITHDRAW_START",
        "END": "LATE_NIGHT_AUTO_WITHDRAW_END",
        "MIN_BALANCE": "LATE_NIGHT_AUTO_WITHDRAW_MIN_BALANCE",
    }
    for nk, ok in legacy.items():
        if ok in config:
            out[nk] = config[ok]
    return out


def _parse_hhmm_local(s: str, default_h: int, default_m: int) -> tuple[int, int]:
    s = (s or "").strip()
    if not s:
        return default_h, default_m
    parts = s.replace(":", " ").split()
    if len(parts) >= 2:
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            pass
    if ":" in s:
        a, b = s.split(":", 1)
        try:
            return int(a), int(b)
        except ValueError:
            pass
    return default_h, default_m


def _is_late_night_auto_withdraw_window(config: dict) -> bool:
    s = _late_night_settings(config)
    if int(s.get("ENABLED", 0)) != 1:
        return False
    now = time.time()
    try:
        dt = datetime.fromtimestamp(now, tz=_LATE_NIGHT_TZ)
    except Exception:
        dt = datetime.fromtimestamp(now, tz=ZoneInfo("Asia/Ho_Chi_Minh"))
    minutes_now = dt.hour * 60 + dt.minute
    sh, sm = _parse_hhmm_local(str(s.get("START", "23:55")), 23, 55)
    eh, em = _parse_hhmm_local(str(s.get("END", "24:00")), 24, 0)
    start_m = sh * 60 + sm
    end_m = eh * 60 + em
    if end_m <= start_m:
        end_m = 24 * 60
    return start_m <= minutes_now < end_m


def _late_night_min_balance(config: dict) -> int:
    try:
        return int(_late_night_settings(config).get("MIN_BALANCE", 0))
    except Exception:
        return 0


def _late_night_session_key(session_id: Any) -> str:
    if session_id is None:
        return "_unknown"
    return str(session_id)


def _prune_late_night_consumed() -> None:
    if len(_late_night_consumed) <= _LATE_NIGHT_CONSUMED_MAX:
        return
    _late_night_consumed.clear()


def _late_night_preliminary_eligible(username: str, balance: int, config: dict) -> bool:
    """Chỉ cần vượt MIN_BALANCE (Late Night) và có mức rút trong thang WITHDRAW_AMOUNTS."""
    if balance <= _late_night_min_balance(config):
        return False
    if not find_nearest_withdraw_amount(balance):
        return False
    return True


def _flush_late_night_session(session_key: str) -> None:
    with _late_night_lock:
        _late_night_timers.pop(session_key, None)
        candidates_map = _late_night_buffer.pop(session_key, {})
        if session_key in _late_night_consumed:
            return
        if not candidates_map:
            return

    config = load_config()
    if int(_late_night_settings(config).get("ENABLED", 0)) != 1:
        return
    if not _is_late_night_auto_withdraw_window(config):
        return

    ranked = sorted(candidates_map.items(), key=lambda kv: kv[1], reverse=True)
    winner: Optional[str] = None
    win_balance: int = 0
    for uname, bal in ranked:
        if not _late_night_preliminary_eligible(uname, bal, config):
            continue
        winner = uname
        win_balance = bal
        break

    if not winner:
        print(
            f"🌙 [AutoWithdraw][LateNight] Phiên {session_key}: không có user đủ điều kiện (>{_late_night_min_balance(config):,}đ + thang rút)",
            flush=True,
        )
        return

    with _late_night_lock:
        if session_key in _late_night_consumed:
            return
        _late_night_consumed.add(session_key)
        _prune_late_night_consumed()

    print(
        f"🌙 [AutoWithdraw][LateNight] Phiên {session_key}: chọn [{winner}] balance={win_balance:,}đ để rút (tối đa 1 user/phiên)",
        flush=True,
    )

    # Không bọc user_lock ở đây: handle_won_session_withdrawal tự khóa ngắn cho pending,
    # rồi gọi API ngoài lock — tránh kẹt Late Night (đã in "chọn" nhưng không "Gửi lệnh rút").
    try:
        result = handle_won_session_withdrawal(winner, win_balance, late_night_flush=True)
        if result.get("withdrew"):
            print(
                f"🌙 [AutoWithdraw][LateNight][{winner}] Đã rút {int(result.get('amount') or 0):,}đ",
                flush=True,
            )
        elif result.get("pending"):
            print(
                f"⏳ [AutoWithdraw][LateNight][{winner}] Chưa rút xong: {result.get('message', 'pending')}",
                flush=True,
            )
        elif result.get("ok") and result.get("message"):
            print(
                f"⏭️ [AutoWithdraw][LateNight][{winner}] {result.get('message')}",
                flush=True,
            )
        elif not result.get("ok"):
            print(
                f"⚠️ [AutoWithdraw][LateNight][{winner}] {result.get('error', result)}",
                flush=True,
            )
    except Exception as e:
        print(f"❌ [AutoWithdraw][LateNight][{winner}] {e}", flush=True)
        import traceback

        traceback.print_exc()


def _schedule_late_night_flush(session_key: str) -> None:
    delay = _LATE_NIGHT_DEBOUNCE_SEC
    with _late_night_lock:
        old = _late_night_timers.pop(session_key, None)
        if old:
            try:
                old.cancel()
            except Exception:
                pass
        t = threading.Timer(delay, _flush_late_night_session, args=[session_key])
        t.daemon = True
        _late_night_timers[session_key] = t
        t.start()


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
    # withdraw_queue do module này quản lý vòng đời (mark/cancel/clear),
    # nên giữ hành vi "source of truth" từ RAM để không sót thao tác xóa.
    state["withdraw_queue"] = pending_withdrawals

    # wager_queue có thể được cập nhật bởi module khác (vd: withdraw.py),
    # vì vậy merge để tránh ghi đè mất dữ liệu vừa được cập nhật.
    file_gq = state.get("wager_queue")
    if not isinstance(file_gq, dict):
        file_gq = {}
    merged_gq = dict(file_gq)
    merged_gq.update(required_bets)
    state["wager_queue"] = merged_gq
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
        cfg_path = _os.path.join(_QUEUE_DIR, "config.json")
        with open(cfg_path, "r", encoding="utf-8") as f:
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
        return int(config.get("WITHDRAW_THRESHOLD_MIN_V1", config.get("WITHDRAW_THRESHOLD_MIN", 300000)))

    if group == "V1":
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
        # Chỉ lấy cụm số tiền ngay sau "vui lòng chơi thêm"
        # để tránh ăn nhầm các số khác có trong message/log.
        import re
        match = re.search(
            r"vui lòng chơi thêm\s+([0-9\.\,\s\u00a0]+)",
            str(error_message or ""),
            re.IGNORECASE,
        )
        if match:
            raw = match.group(1)
            # Giữ lại chữ số, loại bỏ dấu chấm/phẩy/khoảng trắng/NBSP
            digits = re.sub(r"[^\d]", "", raw)
            if digits:
                return int(digits)
    except Exception as e:
        print(f"⚠️ Lỗi parse error message: {e}")
    
    return None


def _is_withdraw_chờ_thêm_message(message: str) -> bool:
    """[-11] chỉ xử lý đặc biệt khi game báo đúng kiểu 'Bạn cần chờ thêm … giây'."""
    return "bạn cần chờ thêm" in str(message or "").lower()


def _parse_wait_seconds_from_withdraw_message(message: str) -> Optional[int]:
    """Parse số giây từ message lỗi rút (vd: 'chờ thêm 8 giây')."""
    try:
        import re

        s = str(message or "")
        m = re.search(r"chờ\s+thêm\s+(\d+)\s*giây", s, re.IGNORECASE)
        if m:
            return int(m.group(1))
        m = re.search(r"thêm\s+(\d+)\s*giây", s, re.IGNORECASE)
        if m:
            return int(m.group(1))
        m = re.search(r"(\d+)\s*giây", s, re.IGNORECASE)
        if m:
            return int(m.group(1))
    except Exception:
        pass
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


def _withdraw_locked_first_then_retry_minus11(
    username: str, amount: int, *, bypass_global_cooldown: bool = False
) -> dict:
    """
    Lần rút đầu: check global cooldown (trừ khi bypass_global_cooldown, vd. Late Night đã chọn 1 user/phiên).
    [-11] + nội dung 'Bạn cần chờ thêm': không sleep giữ lock; trả về delay để caller hẹn retry riêng theo user.
    [-11] khác: trả kết quả như bình thường.
    """
    global last_withdraw_at
    from withdraw import withdraw

    with global_withdraw_lock:
        if not bypass_global_cooldown and not _can_attempt_withdraw_now():
            return {"ok": False, "cooldown": True, "error": "Global cooldown active"}
        result = withdraw(username, amount)
        response_data = result.get("response") or {}
        if not isinstance(response_data, dict):
            response_data = {}
        error_code = _parse_code(response_data.get("code"))
        if error_code in (0, 1):
            last_withdraw_at = time.time()
            return result
        if error_code == -11:
            msg = str(response_data.get("message") or result.get("error") or "")
            if _is_withdraw_chờ_thêm_message(msg):
                wait = _parse_wait_seconds_from_withdraw_message(msg)
                if wait is None or wait < 1:
                    wait = 8
                delay = wait + 2
                print(
                    f"⏳ [AutoWithdraw][{username}] [-11 chờ thêm] Đợi {delay} giây ({wait}s game + 2s đệm) rồi retry riêng user này...",
                    flush=True,
                )
                if isinstance(result, dict):
                    result["retry_after_seconds"] = delay
                    result["pending"] = True
        return result


def _withdraw_with_global_cooldown(
    username: str, amount: int, *, bypass_global_cooldown: bool = False
) -> dict:
    """
    Chỉ set cooldown 60s khi rút thành công (code 0/1).
    [-11] + 'Bạn cần chờ thêm': sleep (N+2) giây rồi rút lại (lần 2 không check cooldown 60s).
    """
    return _withdraw_locked_first_then_retry_minus11(
        username, amount, bypass_global_cooldown=bypass_global_cooldown
    )


def _withdraw_for_pending(username: str, amount: int) -> dict:
    return _withdraw_locked_first_then_retry_minus11(username, amount)


def _get_latest_balance(username: str) -> Optional[int]:
    try:
        from get_balance import get_balance as get_balance_func
        result = get_balance_func(username)
        if isinstance(result, dict) and result.get("ok"):
            return int(result.get("balance") or 0)
    except Exception:
        return None
    return None


def _mark_pending_ready(
    username: str,
    balance: int,
    reason: str = "balance_ready",
    amount_override: Optional[int] = None,
) -> Optional[int]:
    amount = amount_override if amount_override is not None else find_nearest_withdraw_amount(balance)
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


def _defer_pending_retry(username: str, delay_seconds: int, reason: str = "wait_minus11"):
    pending = pending_withdrawals.get(username)
    if not isinstance(pending, dict):
        return
    wait_seconds = max(1, int(delay_seconds))
    pending["next_retry_at"] = time.time() + wait_seconds
    pending["reason"] = reason
    pending_withdrawals[username] = pending
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
    now_ts = time.time()

    # FIFO theo added_at
    candidates = sorted(
        pending_withdrawals.items(),
        key=lambda kv: kv[1].get("added_at", 0)
    )

    for username, pending in candidates:
        next_retry_at = pending.get("next_retry_at")
        if isinstance(next_retry_at, (int, float)) and next_retry_at > now_ts:
            continue

        target_total_bet = required_bets.get(username)
        if isinstance(target_total_bet, int) and target_total_bet > 0:
            current_total = get_total_bet_for_user(username)
            if current_total < target_total_bet:
                _clear_pending_silent(username)
                print(
                    f"⏸️ [AutoWithdraw][{username}] Chưa đủ cược: {current_total:,}/{target_total_bet:,} (skip pending)",
                    flush=True,
                )
                continue
            _clear_required_bet(username)

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

        amount: Optional[int] = None
        try:
            # Re-check: user có thể đã bị clear bởi handle_won_session
            if username not in pending_withdrawals:
                continue
            pend = pending_withdrawals[username]
            balance = _get_latest_balance(username)
            if balance is None:
                balance = pend.get("last_balance", 0)
            amount = pend.get("amount") or find_nearest_withdraw_amount(balance)
        finally:
            # Không giữ user_lock trong lúc gọi API / sleep [-11] — tránh kẹt Late Night flush
            # đang chờ cùng lock (in "chọn ..." nhưng không thấy "Gửi lệnh rút").
            user_lock.release()

        if not amount:
            continue

        user_lock.acquire(blocking=True)
        try:
            if username not in pending_withdrawals:
                continue
        finally:
            user_lock.release()

        print(f"⏳ [AutoWithdraw][{username}] Pending -> Try rút {amount:,}đ")
        result = _withdraw_for_pending(username, amount)

        if result.get("cooldown"):
            return

        response_data = result.get("response", {}) if isinstance(result, dict) else {}
        if not isinstance(response_data, dict):
            response_data = {}
        error_code = _parse_code(response_data.get("code"))
        full_message = response_data.get("message", "")

        if result.get("ok") or error_code in (0, 1):
            _clear_pending_silent(username)
            return

        if error_code == -10:
            combined_err_text = " ".join(
                [str(full_message or ""), str(result.get("error") or "")]
            )
            need_more = parse_required_bet_from_error(combined_err_text)
            if need_more:
                _set_required_bet(username, need_more)
            _clear_pending_silent(username)
            return

        if error_code == -14:
            _cancel_pending_withdrawal(username, "[-14] Số dư không đủ")
            return

        if error_code == -11 and _is_withdraw_chờ_thêm_message(full_message):
            delay = result.get("retry_after_seconds")
            if not isinstance(delay, int):
                delay = _parse_wait_seconds_from_withdraw_message(full_message) or 8
                delay += 2
            _defer_pending_retry(username, delay, reason="wait_minus11")
            continue

        _cancel_pending_withdrawal(
            username,
            f"Code {error_code}" if error_code is not None else "Rút tiền thất bại",
        )
        return

# ================= WITHDRAW LOGIC =================

def _finalize_after_withdraw_api(
    username: str,
    balance: int,
    amount: int,
    result: dict,
) -> dict:
    """Cập nhật pending sau khi gọi API rút (hoặc cooldown); gọi trong user_lock."""
    if result.get("cooldown"):
        _mark_pending_ready(
            username,
            balance,
            reason="cooldown_wait",
        )
        print(
            f"⏳ [AutoWithdraw][{username}] Global cooldown {WITHDRAW_GLOBAL_COOLDOWN_SECONDS}s — đưa vào pending, sẽ thử lại",
            flush=True,
        )
        return {
            "ok": True,
            "withdrew": False,
            "pending": True,
            "message": "Global cooldown active, added to pending list",
        }
    response_data = result.get("response") or {}
    if not isinstance(response_data, dict):
        response_data = {}
    error_code = _parse_code(response_data.get("code"))
    full_message = response_data.get("message", "")
    if result.get("ok") or error_code in (0, 1):
        _clear_pending_silent(username)
        return {
            "ok": True,
            "withdrew": True,
            "amount": amount,
            "message": "Rút tiền thành công",
        }
    if error_code == -14:
        _cancel_pending_withdrawal(username, "[-14] Số dư không đủ")
        return {
            "ok": False,
            "withdrew": False,
            "error": "[-14] Số dư không đủ, hủy pending",
        }
    if error_code == -10:
        combined_err_text = " ".join(
            [str(full_message or ""), str(result.get("error") or "")]
        )
        need_more = parse_required_bet_from_error(combined_err_text)
        if need_more:
            _set_required_bet(username, need_more)
        _clear_pending_silent(username)
        return {
            "ok": False,
            "withdrew": False,
            "error": "[-10] Chưa đủ điều kiện rút, chờ đủ cược",
        }
    if error_code == -11 and _is_withdraw_chờ_thêm_message(full_message):
        delay = result.get("retry_after_seconds")
        if not isinstance(delay, int):
            delay = _parse_wait_seconds_from_withdraw_message(full_message) or 8
            delay += 2
        _defer_pending_retry(username, delay, reason="wait_minus11")
        return {
            "ok": True,
            "withdrew": False,
            "pending": True,
            "message": f"[-11 chờ thêm] Hẹn retry riêng sau {delay}s",
        }

    err_msg = result.get("error", "")
    if err_msg:
        print(f"❌ [{username}] Rút tiền thất bại: {err_msg}", flush=True)
    _cancel_pending_withdrawal(
        username,
        f"Code {error_code}" if error_code is not None else "Rút tiền thất bại",
    )
    return {
        "ok": False,
        "withdrew": False,
        "error": result.get("error") or "Rút tiền thất bại",
    }


def handle_won_session_withdrawal(
    username: str, balance: int, *, late_night_flush: bool = False
) -> dict:
    config = load_config()
    user_group = get_user_group(username)
    threshold = get_withdraw_threshold(user_group)
    threshold_label = f"nhóm {user_group}"
    # Trong khung LATE_NIGHT_AUTO_WITHDRAW (ENABLED + START–END): ưu tiên MIN_BALANCE, không dùng ngưỡng V1/V2.
    if _is_late_night_auto_withdraw_window(config):
        threshold = _late_night_min_balance(config)
        threshold_label = "LATE_NIGHT MIN_BALANCE"

    # Chỉ cần số dư > ngưỡng (theo khung giờ: Late Night hoặc nhóm); không chặn theo mốc cược phía client.
    # 1. Check balance threshold
    if balance <= threshold:
        return {
            "ok": True,
            "withdrew": False,
            "message": f"Balance {balance:,} <= threshold {threshold:,} ({threshold_label}), skip"
        }

    target_total_bet = required_bets.get(username)
    if isinstance(target_total_bet, int) and target_total_bet > 0:
        current_total = get_total_bet_for_user(username)
        if current_total < target_total_bet:
            _clear_pending_silent(username)
            return {
                "ok": True,
                "withdrew": False,
                "message": (
                    f"Chưa đủ tổng cược: {current_total:,}/{target_total_bet:,}, "
                    "đợi đủ cược rồi mới rút"
                ),
            }
        _clear_required_bet(username)

    if username not in processing_users:
        processing_users[username] = threading.Lock()
    user_lock = processing_users[username]

    with user_lock:
        withdraw_amount = find_nearest_withdraw_amount(balance)
        if not withdraw_amount:
            return {
                "ok": True,
                "withdrew": False,
                "message": f"Balance {balance:,} too high (> 2M)",
            }

        if username not in pending_withdrawals:
            marked = _mark_pending_ready(
                username,
                balance,
                reason="balance_ready",
            )
            if marked is None:
                return {
                    "ok": True,
                    "withdrew": False,
                    "message": f"Balance {balance:,} không khớp thang rút",
                }

        if username not in pending_withdrawals:
            return {
                "ok": False,
                "withdrew": False,
                "error": "Mất trạng thái pending sau khi đánh dấu",
            }

        pending = pending_withdrawals[username]
        status = pending.get("status") or "ready"
        if status != "ready":
            _cancel_pending_withdrawal(username, f"invalid pending status: {status}")
            return {
                "ok": True,
                "withdrew": False,
                "message": "Pending status không hợp lệ, đã hủy",
            }

        amount = pending.get("amount") or find_nearest_withdraw_amount(balance)
        if not amount:
            return {
                "ok": False,
                "withdrew": False,
                "error": f"Balance {balance:,} quá cao hoặc quá thấp",
            }

        if late_night_flush:
            print(
                f"💰 [AutoWithdraw][LateNight][{username}] Gửi lệnh rút {amount:,}đ (số dư {balance:,})",
                flush=True,
            )
        else:
            print(f"💰 [{username}] Try rút {amount:,}đ (balance: {balance:,})", flush=True)

    try:
        result = _withdraw_with_global_cooldown(
            username, amount, bypass_global_cooldown=late_night_flush
        )
    except Exception as e:
        return {
            "ok": False,
            "withdrew": False,
            "error": f"Exception when calling withdraw: {e}",
        }

    with user_lock:
        return _finalize_after_withdraw_api(username, balance, amount, result)


# ================= ENTRY POINT (called from ws_events.py) =================

def handle_won_session_auto_withdraw(username: str, balance: int, session_id: Any = None):
    """
    Entry point từ ws_events.py.
    QUAN TRỌNG: Chạy trong thread riêng để không block event loop async

    Khung LATE_NIGHT (mặc định 23:55–24:00 giờ Asia/Ho_Chi_Minh, bật trong
    config LATE_NIGHT_AUTO_WITHDRAW.ENABLED): chỉ rút nếu balance > MIN_BALANCE;
    gom theo session_id, sau debounce chọn đúng
    1 user (balance cao nhất) rồi gọi handle_won_session_withdrawal.
    """
    def _run_in_thread():
        try:
            bal = int(balance) if balance is not None else 0
        except (TypeError, ValueError):
            bal = 0

        config = load_config()
        if _is_late_night_auto_withdraw_window(config):
            if bal <= _late_night_min_balance(config):
                return
            sk = _late_night_session_key(session_id)
            if sk == "_unknown":
                print(
                    "⚠️ [AutoWithdraw][LateNight] Thiếu session_id — không gom phiên, rút như bình thường",
                    flush=True,
                )
                try:
                    result = handle_won_session_withdrawal(username, bal)
                    if result.get("pending"):
                        pass
                    elif not result.get("ok"):
                        print(f"⚠️ [AutoWithdraw][{username}] Error: {result.get('error')}")
                except Exception as e:
                    print(f"❌ [AutoWithdraw][{username}] Exception: {e}")
                    import traceback

                    traceback.print_exc()
                return

            with _late_night_lock:
                if sk in _late_night_consumed:
                    return
                m = _late_night_buffer.setdefault(sk, {})
                prev = m.get(username, 0)
                if bal > prev:
                    m[username] = bal
            _schedule_late_night_flush(sk)
            return

        # Ngày thường
        try:
            result = handle_won_session_withdrawal(username, bal)

            if result.get("pending"):
                pass
            elif not result.get("ok"):
                print(f"⚠️ [AutoWithdraw][{username}] Error: {result.get('error')}")

        except Exception as e:
            print(f"❌ [AutoWithdraw][{username}] Exception: {e}")
            import traceback

            traceback.print_exc()

    thread = threading.Thread(target=_run_in_thread, daemon=True)
    thread.start()


# ================= TEST SCRIPT =================


