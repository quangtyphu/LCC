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
    0. Gửi tracking device (fake_device_tracking)
    1. Check lịch sử nạp
    2. Check lịch sử rút
    3. Check & nhận hòm quà
    3.5. Check & nhận nhiệm vụ tân thủ (tan-thu-vmission)
    4. Check & nhận nhiệm vụ
    5. Check & nhận VIP
    6. Check balance
    Mỗi bước cách nhau 5s.
    """
    results = {}

    # 0. Đảm bảo user có uuid trong DB
    try:
        from fake_device_tracking import fake_device_tracking
        fake_device_tracking(username)
        results['device_tracking'] = 'OK'
    except Exception as e:
        results['device_tracking'] = f'Lỗi: {e}'
    time.sleep(2)

    # 1. Check lịch sử nạp
    try:
        results['deposit_history'] = check_deposit_history(username)
    except Exception as e:
        results['deposit_history'] = f'Lỗi: {e}'
    time.sleep(2)


    # 2. Check lịch sử rút
    try:
        results['withdraw_history'] = check_withdraw_history(username, limit=10)
    except Exception as e:
        results['withdraw_history'] = f'Lỗi: {e}'
    time.sleep(2)
    # 3. Check & nhận hòm quà
    try:
        results['gift_box'] = auto_claim_gifts(username)
    except Exception as e:
        results['gift_box'] = f'Lỗi: {e}'
    time.sleep(2)

    # 3.5. Check & nhận nhiệm vụ tân thủ (tan-thu-vmission)
    try:
        from tan_thu_vmission_service import check_user, claim_user

        tan_res = check_user(username)
        # Tự nhận nếu có level sẵn sàng; theo yêu cầu thì cần isWon=true + status=ready
        claim_user(username, restrict_to_last_check=False)
        results['tan_thu_vmission'] = tan_res or 'OK'
    except Exception as e:
        results['tan_thu_vmission'] = f'Lỗi: {e}'
    time.sleep(2)

    # 4.5. Check & nhận thưởng x10
    try:
        from x10_mission_checker import check_and_claim_x10
        check_and_claim_x10(username)
        results['x10_mission'] = 'OK'
    except Exception as e:
        results['x10_mission'] = f'Lỗi: {e}'
    time.sleep(2)


    # 4. Check & nhận nhiệm vụ
    try:
        results['missions'] = auto_claim_missions(username)
    except Exception as e:
        results['missions'] = f'Lỗi: {e}'
    time.sleep(2)

    # 5. Check & nhận VIP
    try:
        results['vip'] = check_and_claim_vip(username)
    except Exception as e:
        results['vip'] = f'Lỗi: {e}'
    time.sleep(2)

    # 5.5. Đồng bộ hoàn tiền tuần / tháng (papi preview) qua proxy → user_reward_periods
    try:
        from cashback_reward_periods_api import fetch_and_sync_month_cashback, fetch_and_sync_week_cashback
    except Exception as e:
        results['week_cashback'] = {'ok': False, 'error': str(e)}
        results['month_cashback'] = {'ok': False, 'error': str(e)}
    else:
        try:
            results['week_cashback'] = fetch_and_sync_week_cashback(username)
        except Exception as e:
            results['week_cashback'] = {'ok': False, 'error': str(e)}
        try:
            results['month_cashback'] = fetch_and_sync_month_cashback(username)
        except Exception as e:
            results['month_cashback'] = {'ok': False, 'error': str(e)}

    time.sleep(2)

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
