"""
Đồng bộ hoàn tiền (cashback preview tuần + tháng) → bảng user_reward_periods trên CMS.

GET papi.tele68.com/events/stats/cashback/preview qua proxy (game_request_with_retry).

- WEEKLY → weekJustifiedBetAmount, weekCurrentReward
- MONTHLY → monthJustifiedBetAmount, monthCurrentReward

PUT /api/user-reward-periods/:username; nếu 404 → POST (kỳ còn lại gửi 0).
"""

from __future__ import annotations

import requests

from game_api_helper import NODE_SERVER_URL, game_request_with_retry

CASHBACK_PREVIEW_URL = "https://papi.tele68.com/events/stats/cashback/preview"
USER_REWARD_PERIODS_URL = f"{NODE_SERVER_URL}/api/user-reward-periods"

# Origin theo curl trình duyệt lc79b (khác play.lc79.bet trong build_common_headers)
_LC79B_ORIGIN_HEADERS = {
    "origin": "https://lc79b.bet",
    "referer": "https://lc79b.bet/",
}


def _to_int(v) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _sync_week_fields_to_reward_periods(
    username: str,
    justified_bet_amount: int | None,
    current_reward: int | None,
) -> bool:
    """
    Ghi tuần lên user_reward_periods (camelCase như Node hỗ trợ).
    PUT trước; không có dòng → POST với tháng = 0.
    """
    body: dict = {}
    if justified_bet_amount is not None:
        body["weekJustifiedBetAmount"] = justified_bet_amount
    if current_reward is not None:
        body["weekCurrentReward"] = current_reward
    if not body:
        return False

    try:
        r = requests.put(
            f"{USER_REWARD_PERIODS_URL}/{username}",
            json=body,
            timeout=8,
        )
        if r.status_code == 200:
            return True
        if r.status_code != 404:
            print(
                f"⚠️ [{username}] PUT user-reward-periods: {r.status_code} {r.text[:200]}",
                flush=True,
            )
            return False

        # Chưa có bản ghi — tạo mới
        wj = justified_bet_amount if justified_bet_amount is not None else 0
        wr = current_reward if current_reward is not None else 0
        r2 = requests.post(
            USER_REWARD_PERIODS_URL,
            json={
                "username": username,
                "weekJustifiedBetAmount": wj,
                "weekCurrentReward": wr,
                "monthJustifiedBetAmount": 0,
                "monthCurrentReward": 0,
            },
            timeout=8,
        )
        if r2.status_code == 201:
            return True
        if r2.status_code == 409:
            r3 = requests.put(
                f"{USER_REWARD_PERIODS_URL}/{username}",
                json=body,
                timeout=8,
            )
            return r3.status_code == 200
        print(
            f"⚠️ [{username}] POST user-reward-periods: {r2.status_code} {r2.text[:200]}",
            flush=True,
        )
        return False
    except Exception as e:
        print(f"⚠️ [{username}] Lỗi CMS user-reward-periods: {e}", flush=True)
        return False


def _sync_month_fields_to_reward_periods(
    username: str,
    justified_bet_amount: int | None,
    current_reward: int | None,
) -> bool:
    """Ghi tháng lên user_reward_periods. PUT trước; không có dòng → POST với tuần = 0."""
    body: dict = {}
    if justified_bet_amount is not None:
        body["monthJustifiedBetAmount"] = justified_bet_amount
    if current_reward is not None:
        body["monthCurrentReward"] = current_reward
    if not body:
        return False

    try:
        r = requests.put(
            f"{USER_REWARD_PERIODS_URL}/{username}",
            json=body,
            timeout=8,
        )
        if r.status_code == 200:
            return True
        if r.status_code != 404:
            print(
                f"⚠️ [{username}] PUT user-reward-periods (tháng): {r.status_code} {r.text[:200]}",
                flush=True,
            )
            return False

        mj = justified_bet_amount if justified_bet_amount is not None else 0
        mr = current_reward if current_reward is not None else 0
        r2 = requests.post(
            USER_REWARD_PERIODS_URL,
            json={
                "username": username,
                "weekJustifiedBetAmount": 0,
                "weekCurrentReward": 0,
                "monthJustifiedBetAmount": mj,
                "monthCurrentReward": mr,
            },
            timeout=8,
        )
        if r2.status_code == 201:
            return True
        if r2.status_code == 409:
            r3 = requests.put(
                f"{USER_REWARD_PERIODS_URL}/{username}",
                json=body,
                timeout=8,
            )
            return r3.status_code == 200
        print(
            f"⚠️ [{username}] POST user-reward-periods (tháng): {r2.status_code} {r2.text[:200]}",
            flush=True,
        )
        return False
    except Exception as e:
        print(f"⚠️ [{username}] Lỗi CMS user-reward-periods (tháng): {e}", flush=True)
        return False


def _fetch_cashback_current_pair(username: str, time_unit: str) -> dict:
    """
    Gọi preview với timeUnit (WEEKLY | MONTHLY), trả về justifiedBetAmount/currentReward trong current.

    Returns:
        {"ok": True, "justifiedBetAmount": int|None, "currentReward": int|None} hoặc {"ok": False, "error": str}
    """
    resp = game_request_with_retry(
        username,
        "GET",
        CASHBACK_PREVIEW_URL,
        params={"timeUnit": time_unit},
        extra_headers=_LC79B_ORIGIN_HEADERS,
    )
    if not resp or resp.status_code != 200:
        code = resp.status_code if resp else "no_response"
        return {"ok": False, "error": f"API cashback ({time_unit}) lỗi: {code}"}

    try:
        data = resp.json()
    except Exception as e:
        return {"ok": False, "error": f"Parse JSON: {e}"}

    cur = data.get("current") if isinstance(data, dict) else None
    if not isinstance(cur, dict):
        return {"ok": False, "error": "Thiếu current trong response"}

    jb = _to_int(cur.get("justifiedBetAmount"))
    cr = _to_int(cur.get("currentReward"))
    if jb is None and cr is None:
        return {"ok": False, "error": "Không đọc được justifiedBetAmount/currentReward"}

    return {"ok": True, "justifiedBetAmount": jb, "currentReward": cr}


def fetch_and_sync_week_cashback(username: str) -> dict:
    """
    Gọi preview timeUnit=WEEKLY, đọc current.justifiedBetAmount & current.currentReward,
    đồng bộ vào user_reward_periods (week_justified_bet_amount, week_current_reward).

    Returns:
        {"ok": True, "justifiedBetAmount": int, "currentReward": int} hoặc
        {"ok": False, "error": str}
    """
    parsed = _fetch_cashback_current_pair(username, "WEEKLY")
    if not parsed.get("ok"):
        return parsed

    jb = parsed.get("justifiedBetAmount")
    cr = parsed.get("currentReward")

    ok_cms = _sync_week_fields_to_reward_periods(username, jb, cr)
    if not ok_cms:
        return {
            "ok": False,
            "error": "Gọi API OK nhưng không ghi được CMS",
            "justifiedBetAmount": jb,
            "currentReward": cr,
        }

    out: dict = {"ok": True}
    if jb is not None:
        out["justifiedBetAmount"] = jb
    if cr is not None:
        out["currentReward"] = cr
    return out


def fetch_and_sync_month_cashback(username: str) -> dict:
    """
    Gọi preview timeUnit=MONTHLY, đọc current.justifiedBetAmount & current.currentReward,
    đồng bộ vào user_reward_periods (month_justified_bet_amount, month_current_reward).
    """
    parsed = _fetch_cashback_current_pair(username, "MONTHLY")
    if not parsed.get("ok"):
        return parsed

    jb = parsed.get("justifiedBetAmount")
    cr = parsed.get("currentReward")

    ok_cms = _sync_month_fields_to_reward_periods(username, jb, cr)
    if not ok_cms:
        return {
            "ok": False,
            "error": "Gọi API OK nhưng không ghi được CMS",
            "justifiedBetAmount": jb,
            "currentReward": cr,
        }

    out: dict = {"ok": True}
    if jb is not None:
        out["justifiedBetAmount"] = jb
    if cr is not None:
        out["currentReward"] = cr
    return out


if __name__ == "__main__":
    u = input("Username: ").strip()
    if u:
        print("WEEKLY:", fetch_and_sync_week_cashback(u))
        print("MONTHLY:", fetch_and_sync_month_cashback(u))
