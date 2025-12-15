# x10_mission_checker.py
"""
Script kiểm tra và nhận thưởng nhiệm vụ x10 (x-10dep) cho tài khoản game.
- Kiểm tra nhiệm vụ x10 qua API
- Nếu có thưởng chưa nhận, tự động nhận thưởng và cập nhật balance
- Log chỉ khi nhận thành công
"""

from game_api_helper import game_request_with_retry, update_user_balance
import sys

X10_MISSION_URL = "https://wlb.tele68.com/v1/mission/x-10dep"
X10_CLAIM_URL = "https://wsslot.tele68.com/v1/mission/x-10dep"

LEVELS = [
    "k_lva", "k_lvb", "k_lvc", "k_lvd", "k_lve",
    "k_lvf", "k_lvg", "k_lvh", "k_lvi", "k_lvj"
]

def check_and_claim_x10(username):
    resp = game_request_with_retry(username, "GET", X10_MISSION_URL)
    if not resp or resp.status_code != 200:
        return
    try:
        data = resp.json()
    except Exception:
        return
    if not isinstance(data, list) or not data:
        return
    mission = data[0]
    achievement = mission.get("achievement", {})
    records = mission.get("records", [])
    # Tìm các level có thể nhận (achievement[level] != 0)
    for level in LEVELS:
        amount = achievement.get(level, 0)
        if amount:
            # Kiểm tra đã nhận chưa (records có level này và is_claim==0)
            record = next((r for r in records if r["level"] == level and r["is_claim"] == 0), None)
            if record:
                claim_id = record["id"]
                claim_amount = record["amount"]
                # Gọi API nhận thưởng
                claim_resp = game_request_with_retry(
                    username, "PUT", X10_CLAIM_URL, json_data={"id": claim_id}
                )
                if claim_resp and claim_resp.status_code in (200, 201):
                    try:
                        claim_data = claim_resp.json()
                        new_balance = claim_data.get("balance")
                        if new_balance is not None:
                            update_user_balance(username, new_balance)
                        print(f"✅ [{username}] Đã nhận x10 {level}: +{claim_amount:,} | Balance: {new_balance}", flush=True)
                    except Exception:
                        pass

if __name__ == "__main__":
    if len(sys.argv) >= 2:
        username = sys.argv[1]
    else:
        username = input("Nhập username: ").strip()
        if not username:
            print("Chưa nhập username!")
            sys.exit(1)
    check_and_claim_x10(username)
