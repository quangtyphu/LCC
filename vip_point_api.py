from game_api_helper import game_request_with_retry, update_user_balance, get_user_auth

def check_and_claim_vip(username):
    """
    - Check VIP-point
    - Nếu chưa nhận thưởng VIP ở cấp hiện tại thì nhận thưởng
    - Nếu đủ điều kiện đổi điểm (VIP >= 5 và pointExchangeable >= 1) thì đổi điểm
    - Cập nhật balance vào DB sau mỗi lần nhận thưởng hoặc đổi điểm thành công
    """
    # 1. Check VIP-point
    api_url = "https://wlb.tele68.com/v1/lobby/vippoint"
    resp = game_request_with_retry(username, "GET", api_url)
    if not resp or resp.status_code != 200:
        print(f"❌ [{username}] Lỗi lấy thông tin VIP-point: {resp.status_code if resp else 'No response'}", flush=True)
        return False
    try:
        data = resp.json()
        pointExchangeable = data.get("pointExchangeable")
        level = data.get("level")
        bonusClaimed = data.get("bonusClaimed")
    except Exception as e:
        print(f"❌ [{username}] Lỗi parse VIP-point: {e}", flush=True)
        return False

    # 2. Nhận thưởng VIP cho tất cả các cấp chưa nhận từ 1 đến level hiện tại
    if level and bonusClaimed and 1 <= level <= 9:
        for lv in range(1, level+1):
            if not bonusClaimed[lv-1]:
                reward_url = "https://wlb.tele68.com/v1/lobby/vippoint/reward"
                reward_body = {"level": lv}
                resp2 = game_request_with_retry(username, "POST", reward_url, json_data=reward_body)
                if resp2 and resp2.status_code in (200, 201):
                    try:
                        reward_data = resp2.json()
                        balance = reward_data.get("balance")
                        print(f"[{username}] Đã nhận thưởng VIP {lv}, balance mới: {balance}", flush=True)
                        if balance is not None:
                            update_user_balance(username, float(balance))
                    except Exception:
                        pass
    # Không in log đã nhận rồi, không in log tổng quan

    # 3. Đổi điểm VIP nếu đủ điều kiện
    if level and pointExchangeable and level >= 5:
        point_int = int(pointExchangeable)
        if point_int >= 1:
            exchange_url = "https://wlb.tele68.com/v1/lobby/vippoint/exchange"
            exchange_body = {"point": point_int}
            resp3 = game_request_with_retry(username, "POST", exchange_url, json_data=exchange_body)
            if resp3 and resp3.status_code in (200, 201):
                try:
                    exchange_data = resp3.json()
                    balance = exchange_data.get("balance")
                    print(f"[{username}] Đã đổi {point_int} điểm VIP, balance mới: {balance}", flush=True)
                    if balance is not None:
                        update_user_balance(username, float(balance))
                except Exception:
                    pass
    # Không in log không đủ điểm, không in log tổng quan

    return True

if __name__ == "__main__":
    username = input("Nhập username: ").strip()
    if not username:
        print("❌ Username không được để trống")
        exit(1)
    check_and_claim_vip(username)
