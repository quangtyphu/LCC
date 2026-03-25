"""
Scheduler nạp tiền cho user Hết Tiền có streak >= min trong ngày.
(Đã chuyển từ periodic 22:00-23:45 sang event-based: check khi user chuyển Hết Tiền)
"""

import threading
import time
from datetime import datetime

import requests

from constants import load_config
from auto_deposit_on_out_of_money import (
    can_create_deposit_order,
    enqueue_deposit_order,
    is_in_v2_v3,
    _is_v2_auto_deposit_blocked,
)

API_BASE = "http://127.0.0.1:3000"
HET_TIEN_CHECK_DELAY_SECONDS = 20


def check_and_deposit_on_het_tien_if_streak(user: str) -> None:
    """
    Khi user chuyển trạng thái Hết Tiền: delay 20s rồi check dây thắng/dây thua >= HET_TIEN_STREAK_MIN.
    Nếu >= 4 thì nạp tiền ngay. Chạy trong background thread, không block.
    V2/V3/PRIORITY khi bật AUTO_DEPOSIT_V2_V3: không dùng luồng này (tránh nạp 2 lần với auto_deposit_for_user).
    """
    config = load_config()
    if not config:
        return
    if is_in_v2_v3(user, config) and int(config.get("AUTO_DEPOSIT_V2_V3", 0) or 0) == 1:
        return

    def _run():
        time.sleep(HET_TIEN_CHECK_DELAY_SECONDS)
        _check_and_deposit_on_het_tien_if_streak_impl(user)

    threading.Thread(target=_run, daemon=True).start()


def _check_and_deposit_on_het_tien_if_streak_impl(user: str) -> bool:
    """
    Logic thực tế: check dây thắng/dây thua >= HET_TIEN_STREAK_MIN.
    Return True nếu đã enqueue deposit.
    """
    config = load_config()
    if not config:
        return False
    min_streak = int(config.get("HET_TIEN_STREAK_MIN", 4) or 4)
    users = fetch_het_tien_streak_users(min_streak)
    if user not in users:
        return False
    if is_in_v2_v3(user, config):
        v2_users = config.get("PRIORITY_USERS_V2", [])
        v3_users = config.get("PRIORITY_USERS_V3", [])
        if (user in v2_users or user in v3_users) and _is_v2_auto_deposit_blocked(config):
            return False
        if config.get("AUTO_DEPOSIT_V2_V3", 0) != 1:
            return False
        # Đã bật auto nạp V2/V3/P1 → chỉ dùng auto_deposit_on_out_of_money (periodic/_apply), không nạp thêm qua streak
        return False
    else:
        if config.get("AUTO_DEPOSIT_OUTSIDE_V2_V3", 0) != 1:
            return False
    if not can_create_deposit_order(user):
        return False
    enqueue_deposit_order(user)
    print(f"[STREAK] [{user}] Hết tiền + streak>={min_streak} → nạp ngay", flush=True)
    return True


def _normalize_username(item) -> str:
    if isinstance(item, dict):
        return str(item.get("username") or item.get("user") or item.get("id") or "").strip()
    return str(item).strip()


def fetch_het_tien_streak_users(min_streak: int):
    try:
        r = requests.get(
            f"{API_BASE}/api/users/het-tien-streak",
            params={"min": min_streak},
            timeout=8
        )
        if r.status_code != 200:
            print(f"[STREAK] ❌ API het-tien-streak lỗi: {r.status_code}", flush=True)
            return []
        data = r.json()
        items = data.get("data", data) if isinstance(data, dict) else data
        if not isinstance(items, list):
            return []
        users = []
        for item in items:
            u = _normalize_username(item)
            if u:
                users.append(u)
        return users
    except Exception as e:
        print(f"[STREAK] ❌ Lỗi gọi het-tien-streak: {e}", flush=True)
        return []


def deposit_het_tien_streak_users():
    config = load_config()
    if not config:
        print("[STREAK] ❌ Không đọc được config", flush=True)
        return

    min_streak = int(config.get("HET_TIEN_STREAK_MIN", 4) or 4)
    users = fetch_het_tien_streak_users(min_streak)
    if not users:
        return

    for user in users:
        _check_and_deposit_on_het_tien_if_streak_impl(user)


def auto_het_tien_streak_scheduler(interval_seconds=60):
    """
    Chạy mỗi interval_seconds trong khoảng 22:00 - 23:45.
    """
    print("[STREAK] 🕐 Đã khởi động scheduler (22:00-23:45)", flush=True)
    last_run_at = 0.0

    while True:
        try:
            now = datetime.now()
            current_time = now.strftime("%H:%M")

            in_window = ("22:00" <= current_time < "23:45")
            if in_window:
                if time.time() - last_run_at >= interval_seconds:
                    deposit_het_tien_streak_users()
                    last_run_at = time.time()

            time.sleep(60)

        except Exception as e:
            print(f"[STREAK] ❌ Lỗi scheduler: {e}", flush=True)
            time.sleep(60)

