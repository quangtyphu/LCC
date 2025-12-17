import random, json
from typing import List, Tuple, Dict
from datetime import datetime
from zoneinfo import ZoneInfo

heSoNhan = 1000   # hệ số nhân mặc định
MAX_BET = 200000
max_amt = MAX_BET // heSoNhan   # ví dụ 200.000 / 1000 = 200
# ====== Hàm load config ======
def load_config():
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)

# ====== Helper: lấy window đang hiệu lực (giờ VN) ======
def _get_active_window(cfg: dict) -> dict:
    """
    Trả về nguyên item trong TIME_WINDOWS nếu giờ hiện tại thuộc khoảng
    - Inclusive start, exclusive end
    - Hỗ trợ qua nửa đêm khi start > end (vd 20:00 -> 00:00)
    Không khớp thì trả {}
    """
    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    now = datetime.now(tz).time()
    windows = cfg.get("TIME_WINDOWS") or []

    # dùng datetime.strptime để parse HH:MM
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

# ====== Public getters theo khung giờ ======
def get_priority_users() -> List[str]:
    cfg = load_config()
    w = _get_active_window(cfg)
    if "PRIORITY_USERS" in w and isinstance(w["PRIORITY_USERS"], list):
        # Lọc chuỗi rỗng nếu có
        return [u for u in w["PRIORITY_USERS"] if isinstance(u, str) and u.strip()]
    pu = cfg.get("PRIORITY_USERS", [])
    return [u for u in pu if isinstance(u, str) and u.strip()]

def get_assign_strategy(default_value: int = 1) -> int:
    cfg = load_config()
    w = _get_active_window(cfg)
    if "ASSIGN_STRATEGY" in w:
        try:
            return int(w["ASSIGN_STRATEGY"])
        except Exception:
            pass
    try:
        return int(cfg.get("ASSIGN_STRATEGY", default_value))
    except Exception:
        return default_value

# ====== Hàm lấy dải đặt cược (có override theo giờ) ======
def get_bet_range():
    """
    Ưu tiên BET_RANGE trong window nếu có key hợp lệ; phần nào thiếu lấy từ root.
    Nếu window không có/không hợp lệ -> dùng root.
    Cuối cùng bổ sung mặc định để đủ START/STOP/STEP và đúng kiểu int.
    """
    default_bet_range = {"START": 50, "STOP": 71, "STEP": 10}
    config = load_config()
    w = _get_active_window(config)

    # Lấy BET_RANGE gốc từ root (nếu có)
    root_br = config.get("BET_RANGE")
    root_br = root_br if isinstance(root_br, dict) else {}

    # Lấy BET_RANGE từ window (chỉ dùng phần có giá trị int)
    win_br = w.get("BET_RANGE")
    if isinstance(win_br, dict):
        filtered_win = {k: v for k, v in win_br.items() if isinstance(v, int)}
    else:
        filtered_win = {}

    # Merge: bắt đầu từ root, sau đó override bằng window hợp lệ
    bet_range_cfg = dict(root_br)
    bet_range_cfg.update(filtered_win)

    # Điền nốt mặc định & ép kiểu
    for k, v in default_bet_range.items():
        if k not in bet_range_cfg or not isinstance(bet_range_cfg[k], int):
            bet_range_cfg[k] = v

    return bet_range_cfg

# ====== Hàm lấy số người chơi (có thể cho phép override nếu bạn thêm vào TIME_WINDOWS) ======
def get_player_count() -> int:
    config = load_config()
    w = _get_active_window(config)
    # Cho phép override nếu bạn thêm "PLAYER_COUNT" trong từng window
    if "PLAYER_COUNT" in w:
        try:
            return int(w["PLAYER_COUNT"])
        except Exception:
            pass
    try:
        return int(config.get("PLAYER_COUNT", 4))  # mặc định 4 nếu không có
    except Exception:
        return 4

# ====== Hàm chia tiền ======
def _split_amount_for_people(total: int, n_people: int) -> List[int]:
    result = []
    remain = total

    MAX_BET = 200000                # giới hạn trên (không được bằng)
    # amt * heSoNhan must be strictly < MAX_BET
    max_amt = (MAX_BET - 1) // heSoNhan   # ví dụ heSoNhan=1000 -> max_amt = 199

    for i in range(n_people):
        if i == n_people - 1:
            # Người cuối cùng nhận hết phần còn lại nhưng vẫn < 200k
            final_amt = min(remain, max_amt)
            if final_amt > 0:
                result.append(final_amt * heSoNhan)
            break

        # Nếu số dư nhỏ (< 10) thì dồn hết (nhưng vẫn chặn <200k)
        if remain < 10:
            final_amt = min(remain, max_amt)
            if final_amt > 0:
                result.append(final_amt * heSoNhan)
            remain = 0
            break

        # Giới hạn chọn random để không vượt quá 200k (không được bằng)
        max_allowed = min(remain, max_amt)

        # Nếu max_allowed < 10 (không thể chọn giá trị theo step 10),
        # thì gán trực tiếp phần càng nhỏ càng tốt (nếu >0).
        if max_allowed < 10:
            if max_allowed > 0:
                # gán phần tối đa cho user hiện tại (vẫn <200k)
                result.append(max_allowed * heSoNhan)
                remain -= max_allowed
            else:
                # không đủ để gán giá trị hợp lệ (bội 10 nhỏ nhất)
                # dừng vòng và thoát
                break
        else:
            # chọn random theo step 10 trong khoảng hợp lệ
            amt = random.choice(range(10, max_allowed + 1, 10))
            result.append(amt * heSoNhan)
            remain -= amt

    return result

# ====== Hàm phân phối cho devices ======
def distribute_for_devices(devices: List[Dict]) -> List[Tuple[None, int, str]]:
    cfg = load_config()
    w = _get_active_window(cfg)

    # Nếu khung giờ đang PAUSE => không tạo cược
    if w.get("PAUSE"):
        print("⏸️ PAUSE theo khung giờ: không tạo cược.")
        return []

    total_players = get_player_count()

    # Nếu ít hơn 2 người thì không thể chia Tài/Xỉu
    if total_players < 2:
        print("⚠️ PLAYER_COUNT < 2, bỏ qua tạo cược.")
        return []

    # số người chơi bên Tài random từ 1 đến total_players-1
    n_tai = random.randint(4, total_players - 4)
    n_xiu = total_players - n_tai

    bet_range_cfg = get_bet_range()

    # Nếu BET_RANGE vô hiệu (START >= STOP) thì coi như nghỉ
    if not bet_range_cfg or bet_range_cfg["START"] >= bet_range_cfg["STOP"]:
        print("⏸️ Khung giờ nghỉ (BET_RANGE vô hiệu).")
        return []

    total_per_side = random.choice(
        range(bet_range_cfg["START"], bet_range_cfg["STOP"] + 1, bet_range_cfg["STEP"])
    )

    bets: List[Tuple[None, int, str]] = []
    for amt in _split_amount_for_people(total_per_side, n_tai):
        bets.append((None, amt, "TAI"))
    for amt in _split_amount_for_people(total_per_side, n_xiu):
        bets.append((None, amt, "XIU"))
    for _, amt, side in bets:
        if amt >= 200000:
            print(f"⚠️ Bet {amt} ({side}) >= 200k → random lại toàn bộ phiên!")
            return distribute_for_devices(devices)
    return bets

# ====== (Optional) Bạn có thể export các getter này cho module khác dùng ======
# get_priority_users()
# get_assign_strategy()
