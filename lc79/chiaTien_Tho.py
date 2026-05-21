import random, json
from typing import List, Tuple, Dict

heSoNhan = 1000   # hệ số nhân mặc định
MAX_BET = 200000
max_amt = MAX_BET // heSoNhan   # ví dụ 200.000 / 1000 = 200

# Dải cược mặc định (fallback khi TIME_WINDOWS không override)
_DEFAULT_BET_RANGE = {"START": 50, "STOP": 201, "STEP": 10}
# ====== Hàm load config ======
def load_config():
    import os

    cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)

# ====== Helper: lấy window đang hiệu lực (giờ VN) ======
from time_windows import get_active_window as _get_active_window

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
    BET_RANGE mặc định cố định; TIME_WINDOWS có thể override từng khung giờ.
    """
    config = load_config()
    w = _get_active_window(config)

    root_br = dict(_DEFAULT_BET_RANGE)

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
    for k, v in _DEFAULT_BET_RANGE.items():
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

    # Khung PAUSE trong TIME_WINDOWS => không cược (kể cả jackpot cao)
    if w.get("PAUSE"):
        print("⏸️ PAUSE theo khung giờ: không tạo cược.")
        return []

    try:
        from jackpot_night_extend import (
            format_jackpot_gate_skip_reason,
            jackpot_periodic_gate_allows_betting,
        )
    except ImportError:
        jackpot_periodic_gate_allows_betting = None  # type: ignore
        format_jackpot_gate_skip_reason = None  # type: ignore
    if jackpot_periodic_gate_allows_betting and not jackpot_periodic_gate_allows_betting(cfg):
        if format_jackpot_gate_skip_reason:
            print(format_jackpot_gate_skip_reason(cfg, action="không tạo cược"), flush=True)
        else:
            print(
                "⏸️ Jackpot: chưa trên JACKPOT_THRESHOLD hoặc đã dừng sau nổ hũ → không tạo cược.",
                flush=True,
            )
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
