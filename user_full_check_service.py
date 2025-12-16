import time
from check_deposit_history import check_deposit_history
from check_withdraw_history import check_withdraw_history
from gift_box_api import auto_claim_gifts
from mission_api import auto_claim_missions
from vip_point_api import check_and_claim_vip

from get_balance import get_balance
from status_utils import update_status

def user_full_check_logic(username: str) -> dict:
    """
    Thực hiện tuần tự các bước kiểm tra và nhận thưởng cho user:
    1. Check lịch sử nạp
    2. Check lịch sử rút
    3. Check & nhận hòm quà
    4. Check & nhận nhiệm vụ
    5. Check & nhận VIP
    6. Check balance
    Mỗi bước cách nhau 5s.
    """
    results = {}
    # 1. Check lịch sử nạp
    try:
        results['deposit_history'] = check_deposit_history(username)
    except Exception as e:
        results['deposit_history'] = f'Lỗi: {e}'
    time.sleep(5)

    # 2. Check lịch sử rút
    try:
        results['withdraw_history'] = check_withdraw_history(username)
    except Exception as e:
        results['withdraw_history'] = f'Lỗi: {e}'
    time.sleep(5)

    # 3. Check & nhận hòm quà
    try:
        results['gift_box'] = auto_claim_gifts(username)
    except Exception as e:
        results['gift_box'] = f'Lỗi: {e}'
    time.sleep(5)

    # 4. Check & nhận nhiệm vụ
    try:
        results['missions'] = auto_claim_missions(username)
    except Exception as e:
        results['missions'] = f'Lỗi: {e}'
    time.sleep(5)

    # 5. Check & nhận VIP
    try:
        results['vip'] = check_and_claim_vip(username)
    except Exception as e:
        results['vip'] = f'Lỗi: {e}'
    time.sleep(5)

    # 6. Check balance
    try:
        results['balance'] = get_balance(username)
    except Exception as e:
        results['balance'] = f'Lỗi: {e}'

    # 7. Cập nhật trạng thái Đang Chơi
    try:
        status_ok = update_status(username, "Đang Chơi")
        results['update_status'] = 'OK' if status_ok else 'Lỗi khi cập nhật trạng thái'
    except Exception as e:
        results['update_status'] = f'Lỗi: {e}'

    return results
