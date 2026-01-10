import random
import requests
import json
import time
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from constants import load_config

API_BASE = "http://127.0.0.1:3000"  # Node.js server
THIRD_PARTY_API_BASE = "http://127.0.0.1:5000"  # Third party deposit handler

# Cache file để lưu username đã tạo lệnh nạp (tránh tạo 2 lệnh treo gần nhau)
DEPOSIT_CACHE_FILE = "deposit_pending_cache.json"
DEPOSIT_CACHE_DELAY_SECONDS = 60 * 60  # 120 phút = 7200 giây

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
        print(f"[CACHE] Đã xóa {username} khỏi cache")
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
                    print(f"[DEPOSIT] {user} (V2/V3) {amount} - OK | Order ID: {order_id} | Status: {result.get('status', 'PENDING')}")
                    # Lưu vào cache sau khi tạo lệnh thành công
                    cache = load_deposit_cache()
                    cache[user] = time.time()
                    save_deposit_cache(cache)
                    print(f"[CACHE] Đã lưu {user} vào cache (120 phút)")
                else:
                    error = result.get("error", "Unknown error")
                    print(f"[DEPOSIT] {user} (V2/V3) {amount} - FAILED: {error}")
            else:
                try:
                    error_data = r.json()
                    error_msg = error_data.get("error", r.text[:200])
                except:
                    error_msg = r.text[:200]
                print(f"[DEPOSIT] {user} (V2/V3) {amount} - status: {r.status_code}, error: {error_msg}")
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
        active_window = _get_active_window(config)
        max_limit = active_window.get("MAX_ACTIVE_USERS_OUTSIDE_V2_V3")
        if max_limit is None:
            max_limit = config.get("MAX_ACTIVE_USERS_OUTSIDE_V2_V3", 3)
        
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
                            print(f"[DEPOSIT] {acc_name} ({user_type}) {amount} - OK | Order ID: {order_id} | Status: {result.get('status', 'PENDING')}")
                            # Lưu vào cache sau khi tạo lệnh thành công
                            cache = load_deposit_cache()
                            cache[acc_name] = time.time()
                            save_deposit_cache(cache)
                            print(f"[CACHE] Đã lưu {acc_name} vào cache (120 phút)")
                        else:
                            error = result.get("error", "Unknown error")
                            print(f"[DEPOSIT] {acc_name} ({user_type}) {amount} - FAILED: {error}")
                    else:
                        try:
                            error_data = rr.json()
                            error_msg = error_data.get("error", rr.text[:200])
                        except:
                            error_msg = rr.text[:200]
                        print(f"[DEPOSIT] {acc_name} ({user_type}) {amount} - status: {rr.status_code}, error: {error_msg}")
                except Exception as e:
                    print(f"[ERROR] Deposit for {acc_name}: {e}")
                    
        except Exception as e:
            print(f"[ERROR] Fetch out-of-money: {e}")

# Example usage:
if __name__ == "__main__":
    user = "test_user"
    auto_deposit_for_user(user)
