import random
import requests
import json
import time
import os
import threading
from queue import Queue
from datetime import datetime
from zoneinfo import ZoneInfo
from constants import load_config
from status_utils import update_status
from user_full_check_service import user_full_check_logic

API_BASE = "http://127.0.0.1:3000"  # Node.js server
THIRD_PARTY_API_BASE = "http://127.0.0.1:5000"  # Third party deposit handler

# Cache file để lưu username đã tạo lệnh nạp (tránh tạo 2 lệnh treo gần nhau)
DEPOSIT_CACHE_FILE = "deposit_pending_cache.json"
DEPOSIT_CACHE_LOCK_FILE = "deposit_pending_cache.json.lock"
DEPOSIT_CACHE_DELAY_SECONDS = 15 * 60  # 15 phút = 900 giây
DEPOSIT_QUEUE_INTERVAL_SECONDS = 60  # Khoảng cách giữa 2 lệnh nạp liên tục
_CACHE_LOCK_TIMEOUT = 5.0  # Giây chờ lock tối đa

_deposit_queue = Queue()
_deposit_worker_thread = None
_deposit_worker_lock = threading.Lock()
_enqueued_users = set()
_enqueued_lock = threading.Lock()
_last_deposit_time = 0.0
_last_deposit_lock = threading.Lock()

# Lần gọi auto_deposit_for_user khi còn slot (0/1) — dùng để không spam [SKIP] ngay sau khi đã nạp user khác
_OUTSIDE_MAX_ACTIVE_RECENT_ATTEMPT = {}  # username -> time.time()
_OUTSIDE_SKIP_SUPPRESS_SEC = 180

# Sau mỗi lần quyết định nạp (chuỗi streak → MAX_ACTIVE) cho user ngoài V2/V3 — bỏ qua gọi lặp từ periodic/watcher
_OUTSIDE_DECISION_COOLDOWN_SEC = 120
_OUTSIDE_DECISION_LAST = {}  # username -> time.time()


def outside_decision_try_skip(user: str) -> bool:
    """True = bỏ qua (vừa mới xử lý xong, không gọi lặp)."""
    u = (user or "").strip()
    if not u:
        return True
    last = _OUTSIDE_DECISION_LAST.get(u)
    return bool(last and (time.time() - last < _OUTSIDE_DECISION_COOLDOWN_SEC))


def outside_decision_done(user: str) -> None:
    u = (user or "").strip()
    if u:
        _OUTSIDE_DECISION_LAST[u] = time.time()


def _acquire_cache_lock():
    """Lấy lock file (tránh race khi nhiều process đọc/ghi)."""
    start = time.time()
    while True:
        try:
            fd = os.open(DEPOSIT_CACHE_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            if time.time() - start > _CACHE_LOCK_TIMEOUT:
                return False
            time.sleep(0.05)


def _release_cache_lock():
    """Giải phóng lock file."""
    try:
        if os.path.exists(DEPOSIT_CACHE_LOCK_FILE):
            os.remove(DEPOSIT_CACHE_LOCK_FILE)
    except Exception:
        pass


def load_deposit_cache():
    """
    Đọc file JSON cache và trả về dict {username: timestamp}.
    Nếu file không tồn tại hoặc lỗi (vd: Extra data do race) → trả về {} và sửa file.
    """
    if not os.path.exists(DEPOSIT_CACHE_FILE):
        return {}
    if not _acquire_cache_lock():
        return {}  # Không lấy được lock, tránh deadlock
    try:
        with open(DEPOSIT_CACHE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError as e:
        print(f"[WARN] Cache file bị lỗi JSON ({e}), reset về {{}}")
        try:
            with open(DEPOSIT_CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump({}, f)
        except Exception:
            pass
        return {}
    except Exception as e:
        print(f"[WARN] Không đọc được cache file: {e}")
        return {}
    finally:
        _release_cache_lock()


def save_deposit_cache(cache_dict):
    """
    Lưu dict vào file JSON cache (atomic write + lock).
    """
    if not _acquire_cache_lock():
        return
    try:
        tmp_path = DEPOSIT_CACHE_FILE + ".tmp"
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(cache_dict, f, indent=2, ensure_ascii=False)
        try:
            os.replace(tmp_path, DEPOSIT_CACHE_FILE)  # Atomic trên Windows
        except OSError:
            os.rename(tmp_path, DEPOSIT_CACHE_FILE)
    except Exception as e:
        print(f"[ERROR] Không lưu được cache file: {e}")
    finally:
        _release_cache_lock()
        try:
            if os.path.exists(DEPOSIT_CACHE_FILE + ".tmp"):
                os.remove(DEPOSIT_CACHE_FILE + ".tmp")
        except Exception:
            pass

def reset_deposit_cache():
    """
    Reset cache (xóa file cache) - dùng khi khởi động chương trình.
    Giống như pending_withdrawals reset về {} khi restart.
    """
    try:
        if os.path.exists(DEPOSIT_CACHE_FILE):
            os.remove(DEPOSIT_CACHE_FILE)
            print(f"[CACHE] Đã reset cache file khi khởi động chương trình")
        else:
            print(f"[CACHE] Cache file không tồn tại, không cần reset")
    except Exception as e:
        print(f"[WARN] Không reset được cache file: {e}")

def remove_from_deposit_cache(username):
    """
    Xóa username khỏi cache (dùng khi callback "Đã Nạp").
    """
    cache = load_deposit_cache()
    if username in cache:
        del cache[username]
        save_deposit_cache(cache)
        pass
    else:
        print(f"[CACHE] {username} không có trong cache")

def cleanup_deposit_cache():
    """
    Xóa các entry đã quá 120 phút khỏi cache.
    """
    cache = load_deposit_cache()
    if not cache:
        return
    
    now = time.time()
    expired = []
    for username, timestamp in cache.items():
        if now - timestamp >= DEPOSIT_CACHE_DELAY_SECONDS:
            expired.append(username)
    
    if expired:
        for username in expired:
            del cache[username]
        save_deposit_cache(cache)
        print(f"[CACHE] Đã xóa {len(expired)} entry quá 15 phút: {expired}")

def can_create_deposit_order(username):
    """
    Check xem có thể tạo lệnh nạp cho username không.
    Return True nếu không có trong cache (cho phép tạo).
    Return False nếu có trong cache (đang có lệnh treo).
    """
    cleanup_deposit_cache()  # Xóa các entry cũ trước khi check
    cache = load_deposit_cache()
    return username not in cache

def _wait_for_deposit_slot():
    """
    Đảm bảo khoảng cách tối thiểu giữa 2 lệnh nạp liên tục.
    Không quan tâm kết quả lệnh trước đó.
    """
    global _last_deposit_time
    with _last_deposit_lock:
        now = time.time()
        wait_seconds = DEPOSIT_QUEUE_INTERVAL_SECONDS - (now - _last_deposit_time)

    if wait_seconds > 0:
        time.sleep(wait_seconds)

    with _last_deposit_lock:
        _last_deposit_time = time.time()

def _perform_deposit_request(user, amount):
    """
    Gọi API nạp tiền và xử lý cache giống logic cũ.
    """
    try:
        r = requests.post(
            f"{THIRD_PARTY_API_BASE}/create-deposit",
            json={"username": user, "amount": amount},
            timeout=60  # Một lần gọi deposit (không retry NDCK)
        )
        if r.status_code == 200:
            result = r.json()
            if result.get("ok"):
                order_id = result.get("order_id", "N/A")
                # Bỏ log DEPOSIT OK
                # Lưu vào cache sau khi tạo lệnh thành công
                cache = load_deposit_cache()
                cache[user] = time.time()
                save_deposit_cache(cache)
                update_status(user, "Đang Chơi")
                user_full_check_logic(user)
                # Bỏ log CACHE lưu
            else:
                error = result.get("error", "Unknown error")
                # Bỏ log DEPOSIT FAILED
        else:
            try:
                error_data = r.json()
                error_msg = error_data.get("error", r.text[:200])
            except Exception:
                error_msg = r.text[:200]
            # Bỏ log DEPOSIT status lỗi
    except Exception as e:
        print(f"[ERROR] Deposit for {user}: {e}")

def _deposit_queue_worker():
    while True:
        user = None
        task_taken = False
        try:
            task = _deposit_queue.get()
            task_taken = True
            if not task:
                continue
            user = task
            _wait_for_deposit_slot()
            amount = random_amount()
            _perform_deposit_request(user, amount)
        except Exception as e:
            print(f"[ERROR] Deposit queue worker: {e}")
        finally:
            if task_taken:
                try:
                    _deposit_queue.task_done()
                except Exception:
                    pass
            if user:
                with _enqueued_lock:
                    _enqueued_users.discard(user)

def _ensure_deposit_worker():
    global _deposit_worker_thread
    with _deposit_worker_lock:
        if _deposit_worker_thread and _deposit_worker_thread.is_alive():
            return
        _deposit_worker_thread = threading.Thread(target=_deposit_queue_worker, daemon=True)
        _deposit_worker_thread.start()

def enqueue_deposit_order(user):
    """
    Đưa lệnh nạp vào hàng chờ để đảm bảo cách nhau 60s.
    """
    _ensure_deposit_worker()
    with _enqueued_lock:
        if user in _enqueued_users:
            return
        _enqueued_users.add(user)
    _deposit_queue.put(user)

def is_in_v2_v3(user, config):
    v2 = config.get("PRIORITY_USERS_V2", [])
    v3 = config.get("PRIORITY_USERS_V3", [])
    p1 = config.get("PRIORITY_USERS", [])
    return user in v2 or user in v3 or user in p1

def random_amount():
    return random.choice([i for i in range(200_000, 300_000, 10_000)])


def _is_v2_auto_deposit_blocked(cfg: dict) -> bool:
    """
    Trả True nếu trong khung giờ V2_TIME_RULES không cho auto nạp (áp dụng V2/V3).
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
            allow = int(w.get("auto_deposit_allow", 1))
            if not start or not end:
                continue
            s = dt.strptime(start, "%H:%M").time()
            e = dt.strptime(end, "%H:%M").time()
            in_range = (s <= now < e) if s < e else (now >= s or now < e)
            if in_range:
                return allow == 0
        except Exception:
            continue
    return False

def _get_active_window(cfg: dict) -> dict:
    """
    Trả về nguyên window đang hiệu lực (inclusive start, exclusive end).
    Hỗ trợ khoảng qua nửa đêm (start > end).
    Không khớp thì trả {}
    """
    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    now = datetime.now(tz).time()
    windows = cfg.get("TIME_WINDOWS") or []

    # parse HH:MM
    from datetime import datetime as dt
    for w in windows:
        s_raw, e_raw = w.get("start"), w.get("end")
        if not s_raw or not e_raw:
            continue
        try:
            s = dt.strptime(s_raw, "%H:%M").time()
            e = dt.strptime(e_raw, "%H:%M").time()
        except Exception:
            continue

        in_range = (s <= now < e) if s < e else (now >= s or now < e)
        if in_range:
            return w
    return {}

def _get_max_active_users_outside_v2_v3(cfg: dict) -> int:
    """
    Lấy MAX_ACTIVE_USERS_OUTSIDE_V2_V3 theo khung giờ hiện tại nếu có,
    nếu không có thì dùng giá trị mặc định trong config.
    """
    active_window = _get_active_window(cfg) or {}
    if isinstance(active_window, dict) and "MAX_ACTIVE_USERS_OUTSIDE_V2_V3" in active_window:
        value = active_window.get("MAX_ACTIVE_USERS_OUTSIDE_V2_V3")
    else:
        value = cfg.get("MAX_ACTIVE_USERS_OUTSIDE_V2_V3", 3)

    try:
        if value is None:
            raise ValueError("missing")
        return int(value)
    except (TypeError, ValueError):
        return cfg.get("MAX_ACTIVE_USERS_OUTSIDE_V2_V3", 3)

def get_all_users_from_config(config):
    """
    Lấy tất cả user từ config (PRIORITY_USERS, PRIORITY_USERS_V2, PRIORITY_USERS_V3).
    Returns: list of usernames (strings) - tất cả user, không phân biệt V2/V3
    """
    all_users = []
    
    # Lấy từ PRIORITY_USERS
    priority_users = config.get("PRIORITY_USERS", [])
    for user in priority_users:
        if user and isinstance(user, str) and user.strip():
            all_users.append(user.strip())
    
    # Lấy từ PRIORITY_USERS_V2
    v2_users = config.get("PRIORITY_USERS_V2", [])
    for user in v2_users:
        if user and isinstance(user, str) and user.strip():
            all_users.append(user.strip())
    
    # Lấy từ PRIORITY_USERS_V3
    v3_users = config.get("PRIORITY_USERS_V3", [])
    for user in v3_users:
        if user and isinstance(user, str) and user.strip():
            all_users.append(user.strip())
    
    # Loại bỏ duplicate
    return list(set(all_users))

def get_active_users_outside_v2_v3(config):
    """
    Gọi API /api/active-users-with-deposits và lọc ra các user ngoài V2/V3/PRIORITY_USERS.
    Returns: list of usernames (strings) ngoài V2/V3/PRIORITY_USERS
    """
    try:
        r = requests.get(f"{API_BASE}/api/active-users-with-deposits", timeout=5)
        if r.status_code != 200:
            print(f"[WARN] Cannot fetch active-users-with-deposits: {r.status_code}")
            return []
        
        data = r.json()
        users = data if isinstance(data, list) else data.get("data", [])
        
        v2 = config.get("PRIORITY_USERS_V2", [])
        v3 = config.get("PRIORITY_USERS_V3", [])
        p1 = config.get("PRIORITY_USERS", [])
        v2_v3_set = set([u for u in v2 + v3 + p1 if u and u.strip()])
        
        # Lọc user ngoài V2/V3/PRIORITY_USERS
        outside_users = []
        for user_item in users:
            # Parse username từ response (có thể là string hoặc dict)
            if isinstance(user_item, dict):
                username = user_item.get("username") or user_item.get("user") or str(user_item.get("id", ""))
            else:
                username = str(user_item).strip()
            
            if username and username not in v2_v3_set:
                outside_users.append(username)
        
        return outside_users
    except Exception as e:
        print(f"[ERROR] Error fetching active-users-with-deposits: {e}")
        return []

def auto_deposit_for_user(user, prioritize_outside_trigger=False, from_decision_chain=False):
    """
    prioritize_outside_trigger: nếu True, đưa `user` lên đầu danh sách outside hết tiền (ít dùng).
    from_decision_chain: True khi gọi từ run_het_tien_deposit_decision (đã có cooldown ở ngoài).
    """
    config = load_config()
    if is_in_v2_v3(user, config):
        if config.get("AUTO_DEPOSIT_V2_V3", 0) != 1:
            return
        v2_users = config.get("PRIORITY_USERS_V2", [])
        v3_users = config.get("PRIORITY_USERS_V3", [])
        if (user in v2_users or user in v3_users) and _is_v2_auto_deposit_blocked(config):
            return
        # Check xem có thể tạo lệnh nạp không (không có lệnh treo)
        if not can_create_deposit_order(user):
            return
        
        # Call third party deposit API for V2/V3 user (qua hàng chờ 60s)
        enqueue_deposit_order(user)
    else:
        if config.get("AUTO_DEPOSIT_OUTSIDE_V2_V3", 0) != 1:
            return
        if not from_decision_chain and outside_decision_try_skip(user):
            return

        # 1. Kiểm tra số user đang active ngoài V2/V3/PRIORITY_USERS
        active_outside_users = get_active_users_outside_v2_v3(config)
        active_count = len(active_outside_users)
        
        # 2. Lấy MAX_ACTIVE_USERS_OUTSIDE_V2_V3 từ TIME_WINDOWS nếu có, nếu không thì dùng giá trị mặc định
        max_limit = _get_max_active_users_outside_v2_v3(config)
        
        u_log = (user or "").strip() or "?"

        # 3. Đủ slot: bỏ qua log nếu vừa mới xét khi còn chỗ (lần sau 1/1 là hệ quả đã nạp user khác)
        if active_count >= max_limit:
            last_try = _OUTSIDE_MAX_ACTIVE_RECENT_ATTEMPT.get(u_log)
            if last_try and (time.time() - last_try < _OUTSIDE_SKIP_SUPPRESS_SEC):
                return
            print(
                f"[SKIP] [{u_log}] MAX_ACTIVE ngoài V2/V3 đã đủ: {active_count}/{max_limit}, không nạp thêm.",
                flush=True,
            )
            return

        _OUTSIDE_MAX_ACTIVE_RECENT_ATTEMPT[u_log] = time.time()

        # 4. Tính số user cần nạp
        need_deposit = max_limit - active_count
        
        # 5. Lấy danh sách user "Hết Tiền" từ API
        try:
            r = requests.get(f"{API_BASE}/api/accounts/out-of-money", timeout=5)
            if r.status_code != 200:
                print(f"[ERROR] Cannot fetch out-of-money accounts.")
                return

            data = r.json()
            accounts = data if isinstance(data, list) else data.get("data", [])
            v2 = config.get("PRIORITY_USERS_V2", [])
            v3 = config.get("PRIORITY_USERS_V3", [])
            p1 = config.get("PRIORITY_USERS", [])
            v2_v3_set = set([u for u in v2 + v3 + p1 if u and u.strip()])

            # Chuẩn hóa thứ tự tên account (giữ nguyên thứ tự API)
            account_names = []
            for acc in accounts:
                if isinstance(acc, dict):
                    acc_name = acc.get("username") or acc.get("user") or str(acc.get("id", ""))
                else:
                    acc_name = str(acc).strip()
                if acc_name:
                    account_names.append(acc_name)

            u_trigger = (user or "").strip()
            if (
                prioritize_outside_trigger
                and u_trigger
                and u_trigger not in v2_v3_set
                and u_trigger in account_names
            ):
                account_names = [u_trigger] + [x for x in account_names if x != u_trigger]

            # 6. Duyệt danh sách, nạp V2/V3 và đủ số lượng outside
            users_to_deposit = []
            outside_count = 0  # Đếm số user outside đã thêm

            for acc_name in account_names:
                # Nếu là V2/V3 → nạp luôn (không giới hạn)
                if acc_name in v2_v3_set:
                    users_to_deposit.append(acc_name)
                # Nếu là outside và chưa đủ số lượng → nạp
                elif outside_count < need_deposit:
                    users_to_deposit.append(acc_name)
                    outside_count += 1

                # Nếu đã đủ số lượng outside → dừng
                if outside_count >= need_deposit:
                    break
            
            if not users_to_deposit:
                return

            enqueued = []
            for acc_name in users_to_deposit:
                if not can_create_deposit_order(acc_name):
                    continue
                enqueue_deposit_order(acc_name)
                enqueued.append(acc_name)

            if enqueued:
                print(
                    f"[OUTSIDE] [{u_log}] MAX_ACTIVE lúc xét: {active_count}/{max_limit} "
                    f"→ nạp: {', '.join(enqueued)}",
                    flush=True,
                )
            else:
                print(
                    f"[SKIP] [{u_log}] MAX_ACTIVE {active_count}/{max_limit} nhưng không tạo được lệnh "
                    f"(cache treo hoặc không còn slot trong danh sách).",
                    flush=True,
                )

        except Exception as e:
            print(f"[ERROR] Fetch out-of-money: {e}")
        finally:
            if not from_decision_chain:
                outside_decision_done(user)

def periodic_check_all_users():
    """
    Check định kỳ (mặc định mỗi 5 phút):
    - V2/V3/PRIORITY: vẫn gọi auto_deposit khi cần.
    - Outside: chỉ khi PERIODIC_OUTSIDE_DEPOSIT=1 (mặc định 0). Nạp ngoài V2/V3 đã có luồng
      schedule_het_tien / delayed; periodic outside dễ gọi trùng lần 2.
    """
    
    try:
        config = load_config()
        
        # Lấy danh sách V2/V3/PRIORITY từ config để phân loại
        v2 = config.get("PRIORITY_USERS_V2", [])
        v3 = config.get("PRIORITY_USERS_V3", [])
        p1 = config.get("PRIORITY_USERS", [])
        v2_v3_set = set([u for u in v2 + v3 + p1 if u and u.strip()])
        
        
        # Gọi API để lấy danh sách user có trạng thái "Hết Tiền"
        try:
            r = requests.get(f"{API_BASE}/api/accounts/out-of-money", timeout=5)
            if r.status_code != 200:
                print(f"[PERIODIC] Không thể lấy danh sách user hết tiền: status {r.status_code}")
                return
            
            data = r.json()
            accounts = data if isinstance(data, list) else data.get("data", [])
            
            if not accounts:
                return
            
            
            # Phân loại user: V2/V3/PRIORITY và outside (lấy TẤT CẢ user từ API, không filter theo config)
            v2_v3_users = []
            outside_users = []
            
            for acc in accounts:
                # Parse account name: có thể là string hoặc dict
                if isinstance(acc, dict):
                    acc_name = acc.get("username") or acc.get("user") or str(acc.get("id", ""))
                else:
                    acc_name = str(acc).strip()
                
                if not acc_name:
                    continue
                
                # Phân loại V2/V3/PRIORITY hoặc outside dựa vào config
                if acc_name in v2_v3_set:
                    v2_v3_users.append(acc_name)
                else:
                    # Tất cả user không phải V2/V3/PRIORITY đều là outside
                    outside_users.append(acc_name)
            
            
            # ========== Xử lý V2/V3/PRIORITY ==========
            if v2_v3_users:
                if config.get("AUTO_DEPOSIT_V2_V3", 0) == 1:
                    for user in v2_v3_users:
                        try:
                            # Check cache: nếu có trong cache (đang có lệnh treo) → bỏ qua, đợi lần check tiếp theo
                            if not can_create_deposit_order(user):
                                continue
                            
                            # Nếu không có trong cache → gọi auto_deposit_for_user
                            auto_deposit_for_user(user)
                        except Exception as e:
                            print(f"[PERIODIC] Lỗi khi nạp tiền cho {user} (V2/V3/PRIORITY): {e}")
            
            # ========== Outside (tắt mặc định — tránh trùng luồng hết tiền) ==========
            if outside_users:
                if (
                    config.get("AUTO_DEPOSIT_OUTSIDE_V2_V3", 0) == 1
                    and int(config.get("PERIODIC_OUTSIDE_DEPOSIT", 0) or 0) == 1
                ):
                    # 1. Kiểm tra số user đang active ngoài V2/V3
                    active_outside_users = get_active_users_outside_v2_v3(config)
                    active_count = len(active_outside_users)
                    
                    # 2. Lấy MAX_ACTIVE_USERS_OUTSIDE_V2_V3 từ TIME_WINDOWS nếu có, nếu không thì dùng giá trị mặc định
                    max_limit = _get_max_active_users_outside_v2_v3(config)
                    
                    # 3. Nếu đã đủ limit → skip
                    if active_count < max_limit:
                        # 4. Tính số user cần nạp
                        need_deposit = max_limit - active_count
                        
                        # 5. Chọn user cần nạp với logic check cache
                        users_to_deposit = []
                        for user in outside_users:
                            # Check cache: nếu có trong cache (đang có lệnh treo) → loại bỏ, chọn user tiếp theo
                            if not can_create_deposit_order(user):
                                continue
                            
                            # Nếu không có trong cache → thêm vào danh sách nạp
                            users_to_deposit.append(user)
                            
                            # Dừng khi đủ số lượng cần nạp
                            if len(users_to_deposit) >= need_deposit:
                                break
                        
                        # 6. Một lần gọi đủ: auto_deposit_for_user đã quét /api/accounts/out-of-money;
                        # gọi lặp từng user chỉ lặp lại cùng một kiểm tra limit + log SKIP.
                        if users_to_deposit:
                            try:
                                auto_deposit_for_user(
                                    users_to_deposit[0], prioritize_outside_trigger=False
                                )
                            except Exception as e:
                                print(f"[PERIODIC] Lỗi khi nạp tiền outside (batch): {e}")
            
        except Exception as e:
            print(f"[PERIODIC] Lỗi khi gọi API out-of-money: {e}")
        
    except Exception as e:
        print(f"[PERIODIC] Lỗi trong periodic_check_all_users: {e}")

def start_periodic_check(interval_seconds=300):
    """
    Khởi động thread để check định kỳ mỗi interval_seconds giây (mặc định 5 phút = 300 giây).
    """
    def periodic_worker():
        while True:
            try:
                periodic_check_all_users()
            except Exception as e:
                print(f"[PERIODIC] Lỗi trong periodic_worker: {e}")
            
            # Đợi interval_seconds trước khi check lần tiếp theo
            time.sleep(interval_seconds)
    
    thread = threading.Thread(target=periodic_worker, daemon=True)
    thread.start()
    print(f"[PERIODIC] Đã khởi động periodic check mỗi {interval_seconds} giây ({interval_seconds // 60} phút)")
    return thread

# Example usage:
if __name__ == "__main__":
    # Reset cache khi khởi động
    reset_deposit_cache()
    
    # Khởi động periodic check (mỗi 60 giây)
    start_periodic_check(interval_seconds=60)  # 60 giây = 1 phút
    
    # Giữ chương trình chạy
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("\n[INFO] Đang dừng chương trình...")
