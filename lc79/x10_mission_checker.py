# x10_mission_checker.py
"""
Script kiểm tra và nhận thưởng nhiệm vụ x10 (x-10dep) cho tài khoản game.
- Kiểm tra nhiệm vụ x10 qua API
- Nếu có thưởng chưa nhận, tự động nhận thưởng và cập nhật balance
- Log chỉ khi nhận thành công
- Đồng bộ total_bet_amount + mốc roadMap tiếp theo → CMS user_vip_x10_params (x10_total_bet, x10_next_reward_total_bet; cả hai >= 0, gồm 0)
"""

import os
import sys

import requests

from game_api_helper import game_request_with_retry, update_user_balance

X10_MISSION_URL = "https://wlb.tele68.com/v1/mission/x-10dep"
X10_CLAIM_URL = "https://wsslot.tele68.com/v1/mission/x-10dep"

LEVELS = [
    "k_lva", "k_lvb", "k_lvc", "k_lvd", "k_lve",
    "k_lvf", "k_lvg", "k_lvh", "k_lvi", "k_lvj"
]

CMS_API_BASE = os.environ.get("CMS_API_BASE", "http://127.0.0.1:3000")


def _to_int_amount(val) -> int:
    try:
        return int(round(float(val)))
    except (TypeError, ValueError):
        return 0


def compute_x10_next_reward_total_bet(total_bet_amount, road_map: dict) -> int:
    """
    Mốc roadMap đầu tiên (theo thứ tự k_lva → k_lvj) có giá trị > total_bet_amount.
    Nếu đã vượt hết mốc, trả mốc cuối cùng trong roadMap (k_lvj).
    """
    if not isinstance(road_map, dict):
        return 0
    total = float(total_bet_amount or 0)
    ordered: list[float] = []
    for key in LEVELS:
        raw = road_map.get(key)
        if raw is None:
            continue
        try:
            ordered.append(float(raw))
        except (TypeError, ValueError):
            continue
    if not ordered:
        return 0
    for th in ordered:
        if th > total:
            return int(round(th))
    return int(round(ordered[-1]))


def sync_x10_params_to_cms(username: str, total_bet_amount, road_map: dict) -> bool:
    total_i = _to_int_amount(total_bet_amount)
    next_i = compute_x10_next_reward_total_bet(total_bet_amount, road_map)
    try:
        url = f"{CMS_API_BASE}/api/vip-x10-params/{username}"
        r = requests.put(
            url,
            json={
                "x10_total_bet": total_i,
                "x10_next_reward_total_bet": next_i,
            },
            timeout=8,
        )
        if not r.ok:
            print(
                f"⚠️ [{username}] Đồng bộ x10→CMS HTTP {r.status_code}: {r.text[:180]}",
                flush=True,
            )
            return False
        return True
    except Exception as e:
        print(f"⚠️ [{username}] Đồng bộ x10→CMS: {e}", flush=True)
        return False


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
    road_map = mission.get("roadMap") or mission.get("road_map") or {}
    total_bet = mission.get("total_bet_amount")

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

    # Một GET x-10dep đã có đủ total_bet_amount + roadMap → chỉ cần đồng bộ CMS một lần (không GET/PUT lặp).
    sync_x10_params_to_cms(username, total_bet, road_map if isinstance(road_map, dict) else {})


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        username = sys.argv[1]
    else:
        username = input("Nhập username: ").strip()
        if not username:
            print("Chưa nhập username!")
            sys.exit(1)
    check_and_claim_x10(username)
