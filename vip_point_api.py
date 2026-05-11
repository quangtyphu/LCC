import os
import requests

from game_api_helper import game_request_with_retry, update_user_balance

CMS_API_BASE = os.environ.get("CMS_API_BASE", "http://127.0.0.1:3000")


def _vip_point_to_db_int(point_raw) -> int | None:
    """API vippoint trả point float → lưu DB số nguyên (làm tròn)."""
    if point_raw is None:
        return None
    try:
        n = int(round(float(point_raw)))
    except (TypeError, ValueError):
        return None
    return max(0, n)


def sync_vip_point_to_cms(username: str, point_raw) -> bool:
    """
    Cập nhật điểm VIP (point) vào CMS: cột `vip` trong user_vip_x10_params
    qua PUT /api/vip-x10-params/:username (server.js).
    Đã có dòng thì UPDATE; chưa có (GET 404) vẫn PUT để upsert (server dùng default x10_next = 0).
    """
    vip_int = _vip_point_to_db_int(point_raw)
    if vip_int is None:
        return False
    try:
        url = f"{CMS_API_BASE}/api/vip-x10-params/{username}"
        chk = requests.get(url, timeout=6)
        if chk.status_code == 200:
            try:
                payload = chk.json()
                row = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(row, dict):
                    cur = _vip_point_to_db_int(row.get("vip"))
                    if cur is not None and cur == vip_int:
                        return True
            except Exception:
                pass
        elif chk.status_code not in (404, 200):
            print(
                f"⚠️ [{username}] Đọc vip-x10-params HTTP {chk.status_code}: {chk.text[:120]}",
                flush=True,
            )
            return False

        r = requests.put(url, json={"vip": vip_int}, timeout=8)
        if not r.ok:
            print(
                f"⚠️ [{username}] Đồng bộ VIP point→CMS (vip={vip_int}) HTTP {r.status_code}: {r.text[:160]}",
                flush=True,
            )
            return False
        return True
    except Exception as e:
        print(f"⚠️ [{username}] Đồng bộ VIP point→CMS: {e}", flush=True)
        return False


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
        point = data.get("point")
        pointExchangeable = data.get("pointExchangeable")
        level = data.get("level")
        bonusClaimed = data.get("bonusClaimed")
    except Exception as e:
        print(f"❌ [{username}] Lỗi parse VIP-point: {e}", flush=True)
        return False

    sync_vip_point_to_cms(username, point)

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

    resp_final = game_request_with_retry(username, "GET", api_url)
    if resp_final and resp_final.status_code == 200:
        try:
            data_final = resp_final.json()
            sync_vip_point_to_cms(username, data_final.get("point"))
        except Exception:
            pass

    return True

if __name__ == "__main__":
    username = input("Nhập username: ").strip()
    if not username:
        print("❌ Username không được để trống")
        exit(1)
    check_and_claim_vip(username)
