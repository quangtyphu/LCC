import random
import requests
import json
import time
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from constants import load_config

API_BASE = "http://127.0.0.1:3000"  # Node.js server
THIRD_PARTY_API_BASE = "http://127.0.0.1:5000"  # Third party deposit handler

# Cache file để lưu username đã tạo lệnh nạp (tránh tạo 2 lệnh treo gần nhau)
DEPOSIT_CACHE_FILE = "deposit_pending_cache.json"
DEPOSIT_CACHE_DELAY_SECONDS = 15 * 60  # 15 phút = 900 giây

def load_deposit_cache():
    """
    Đọc file JSON cache và trả về dict {username: timestamp}.
    Nếu file không tồn tại hoặc lỗi → trả về {}.
    """
    if not os.path.exists(DEPOSIT_CACHE_FILE):
        return {}
    try:
        with open(DEPOSIT_CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARN] Không đọc được cache file: {e}")
        return {}

def save_deposit_cache(cache_dict):
    """
    Lưu dict vào file JSON cache.
    """
    try:
        with open(DEPOSIT_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache_dict, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[ERROR] Không lưu được cache file: {e}")

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
        print(f"[CACHE] Đã xóa {len(expired)} entry quá 120 phút: {expired}")

def can_create_deposit_order(username):
    """
    Check xem có thể tạo lệnh nạp cho username không.
    Return True nếu không có trong cache (cho phép tạo).
    Return False nếu có trong cache (đang có lệnh treo).
    """
    cleanup_deposit_cache()  # Xóa các entry cũ trước khi check
    cache = load_deposit_cache()
    return username not in cache

def is_in_v2_v3(user, config):
    v2 = config.get("PRIORITY_USERS_V2", [])
    v3 = config.get("PRIORITY_USERS_V3", [])
    return user in v2 or user in v3

def random_amount():
    return random.choice([i for i in range(200_000, 300_000, 10_000)])

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
    Gọi API /api/active-users-with-deposits và lọc ra các user ngoài V2/V3.
    Returns: list of usernames (strings) ngoài V2/V3
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
        v2_v3_set = set([u for u in v2 + v3 if u and u.strip()])
        
        # Lọc user ngoài V2/V3
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

def auto_deposit_for_user(user):
    config = load_config()
    if is_in_v2_v3(user, config):
        if config.get("AUTO_DEPOSIT_V2_V3", 0) != 1:
            print(f"[SKIP] {user} in V2/V3, AUTO_DEPOSIT_V2_V3 is off.")
            return
        # Check xem có thể tạo lệnh nạp không (không có lệnh treo)
        if not can_create_deposit_order(user):
            print(f"[SKIP] {user} đang có lệnh treo trong cache, bỏ qua")
            return
        
        amount = random_amount()
        # Call third party deposit API for V2/V3 user
        try:
            r = requests.post(f"{THIRD_PARTY_API_BASE}/create-deposit", json={"username": user, "amount": amount}, timeout=30)
            if r.status_code == 200:
                result = r.json()
                if result.get("ok"):
                    order_id = result.get("order_id", "N/A")
                    # Bỏ log DEPOSIT OK
                    # Lưu vào cache sau khi tạo lệnh thành công
                    cache = load_deposit_cache()
                    cache[user] = time.time()
                    save_deposit_cache(cache)
                    # Bỏ log CACHE lưu
                else:
                    error = result.get("error", "Unknown error")
                    # Bỏ log DEPOSIT FAILED
            else:
                try:
                    error_data = r.json()
                    error_msg = error_data.get("error", r.text[:200])
                except:
                    error_msg = r.text[:200]
                # Bỏ log DEPOSIT status lỗi
        except Exception as e:
            print(f"[ERROR] Deposit for {user}: {e}")
    else:
        if config.get("AUTO_DEPOSIT_OUTSIDE_V2_V3", 0) != 1:
            print(f"[SKIP] {user} not in V2/V3, AUTO_DEPOSIT_OUTSIDE_V2_V3 is off.")
            return
        
        # 1. Kiểm tra số user đang active ngoài V2/V3
        active_outside_users = get_active_users_outside_v2_v3(config)
        active_count = len(active_outside_users)
        
        # 2. Lấy MAX_ACTIVE_USERS_OUTSIDE_V2_V3 từ TIME_WINDOWS nếu có, nếu không thì dùng giá trị mặc định
        max_limit = _get_max_active_users_outside_v2_v3(config)
        
        print(f"[INFO] Active users outside V2/V3: {active_count}/{max_limit}")
        
        # 3. Nếu đã đủ limit → skip
        if active_count >= max_limit:
            print(f"[SKIP] Đã đủ {active_count} user ngoài V2/V3 đang active (limit: {max_limit}), không nạp thêm.")
            return
        
        # 4. Tính số user cần nạp
        need_deposit = max_limit - active_count
        print(f"[INFO] Cần nạp thêm {need_deposit} user để đạt limit {max_limit}")
        
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
            v2_v3_set = set([u for u in v2 + v3 if u and u.strip()])
            
            # 6. Duyệt danh sách từ đầu, nạp V2/V3 và đủ số lượng outside
            users_to_deposit = []
            outside_count = 0  # Đếm số user outside đã thêm
            
            for acc in accounts:
                # Parse account name: có thể là string hoặc dict
                if isinstance(acc, dict):
                    acc_name = acc.get("username") or acc.get("user") or str(acc.get("id", ""))
                else:
                    acc_name = str(acc).strip()
                
                if not acc_name:
                    continue
                
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
                print(f"[SKIP] Không có user nào trong danh sách 'Hết Tiền' để nạp")
                return
            
            print(f"[INFO] Sẽ nạp tiền cho {len(users_to_deposit)} user: {users_to_deposit}")
            
            # 8. Nạp tiền cho từng user
            for acc_name in users_to_deposit:
                # Check xem có thể tạo lệnh nạp không (không có lệnh treo)
                if not can_create_deposit_order(acc_name):
                    print(f"[SKIP] {acc_name} đang có lệnh treo trong cache, bỏ qua")
                    continue
                
                amount = random_amount()
                # Xác định loại user để log
                user_type = "V2/V3" if acc_name in v2_v3_set else "outside V2/V3"
                try:
                    rr = requests.post(f"{THIRD_PARTY_API_BASE}/create-deposit", json={"username": acc_name, "amount": amount}, timeout=30)
                    if rr.status_code == 200:
                        result = rr.json()
                        if result.get("ok"):
                            order_id = result.get("order_id", "N/A")
                            # Bỏ log DEPOSIT OK
                            # Lưu vào cache sau khi tạo lệnh thành công
                            cache = load_deposit_cache()
                            cache[acc_name] = time.time()
                            save_deposit_cache(cache)
                            # Bỏ log CACHE lưu
                        else:
                            error = result.get("error", "Unknown error")
                            # Bỏ log DEPOSIT FAILED
                    else:
                        try:
                            error_data = rr.json()
                            error_msg = error_data.get("error", rr.text[:200])
                        except:
                            error_msg = rr.text[:200]
                        # Bỏ log DEPOSIT status lỗi
                except Exception as e:
                    print(f"[ERROR] Deposit for {acc_name}: {e}")
                    
        except Exception as e:
            print(f"[ERROR] Fetch out-of-money: {e}")

def periodic_check_all_users():
    """
    Hàm check định kỳ mỗi 5 phút:
    - Gọi API /api/accounts/out-of-money để lấy user có trạng thái "Hết Tiền"
    - So sánh với config để lấy user cần nạp tiền
    - Phân loại V2/V3 và outside, xử lý theo logic riêng
    """
    
    try:
        config = load_config()
        
        # Lấy danh sách V2/V3 từ config để phân loại
        v2 = config.get("PRIORITY_USERS_V2", [])
        v3 = config.get("PRIORITY_USERS_V3", [])
        v2_v3_set = set([u for u in v2 + v3 if u and u.strip()])
        
        
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
            
            
            # Phân loại user: V2/V3 và outside (lấy TẤT CẢ user từ API, không filter theo config)
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
                
                # Phân loại V2/V3 hoặc outside dựa vào config
                if acc_name in v2_v3_set:
                    v2_v3_users.append(acc_name)
                else:
                    # Tất cả user không phải V2/V3 đều là outside
                    outside_users.append(acc_name)
            
            
            # ========== Xử lý V2/V3 ==========
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
                            print(f"[PERIODIC] Lỗi khi nạp tiền cho {user} (V2/V3): {e}")
            
            # ========== Xử lý outside ==========
            if outside_users:
                if config.get("AUTO_DEPOSIT_OUTSIDE_V2_V3", 0) == 1:
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
                        
                        # 6. Nạp tiền cho từng user outside đã được chọn
                        for user in users_to_deposit:
                            try:
                                auto_deposit_for_user(user)
                            except Exception as e:
                                print(f"[PERIODIC] Lỗi khi nạp tiền cho {user} (outside): {e}")
            
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
