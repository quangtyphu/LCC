"""
Scheduler gọi API /api/users/active-no-deposit-today lúc 23:00 mỗi ngày
và tự động đưa user vào hàng chờ nạp tiền.
"""

import time
from datetime import datetime

import requests

from constants import load_config, save_config
from auto_deposit_on_out_of_money import (
    can_create_deposit_order,
    enqueue_deposit_order,
    is_in_v2_v3,
    _is_v2_auto_deposit_blocked,
)

API_BASE = "http://127.0.0.1:3000"


def _normalize_username(item) -> str:
    if isinstance(item, dict):
        return str(item.get("username") or item.get("user") or item.get("id") or "").strip()
    return str(item).strip()


def fetch_active_no_deposit_users():
    try:
        r = requests.get(f"{API_BASE}/api/users/active-no-deposit-today", timeout=8)
        if r.status_code != 200:
            print(f"[NO-DEPOSIT] ❌ API lỗi: {r.status_code} {r.text[:200]}", flush=True)
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
        print(f"[NO-DEPOSIT] ❌ Lỗi gọi API: {e}", flush=True)
        return []


def deposit_active_no_deposit_users():
    config = load_config()
    if not config:
        print("[NO-DEPOSIT] ❌ Không đọc được config", flush=True)
        return

    users = fetch_active_no_deposit_users()
    if not users:
        return

    for user in users:
        if not user:
            continue

        if is_in_v2_v3(user, config):
            v2_users = config.get("PRIORITY_USERS_V2", [])
            v3_users = config.get("PRIORITY_USERS_V3", [])
            if (user in v2_users or user in v3_users) and _is_v2_auto_deposit_blocked(config):
                continue
            if config.get("AUTO_DEPOSIT_V2_V3", 0) != 1:
                continue
            if not can_create_deposit_order(user):
                continue
            enqueue_deposit_order(user)
        else:
            if config.get("AUTO_DEPOSIT_OUTSIDE_V2_V3", 0) != 1:
                continue
            if not can_create_deposit_order(user):
                continue
            enqueue_deposit_order(user)


def auto_active_no_deposit_scheduler():
    """
    Chạy mỗi 60s, nếu đến 23:00 sẽ gọi API và enqueue nạp tiền.
    """
    print("[NO-DEPOSIT] 🕐 Đã khởi động scheduler (23:00 mỗi ngày)", flush=True)

    while True:
        try:
            now = datetime.now()
            current_time = now.strftime("%H:%M")
            current_date = now.strftime("%Y-%m-%d")

            if current_time in ["23:30", "23:31"]:
                config = load_config()
                last_run_date = config.get("LAST_ACTIVE_NO_DEPOSIT_RUN", "")

                if last_run_date != current_date:
                    print("[NO-DEPOSIT] ⏰ 23:00 - Bắt đầu lấy user chưa nạp hôm nay", flush=True)
                    deposit_active_no_deposit_users()
                    config["LAST_ACTIVE_NO_DEPOSIT_RUN"] = current_date
                    save_config(config)

            time.sleep(60)

        except Exception as e:
            print(f"[NO-DEPOSIT] ❌ Lỗi scheduler: {e}", flush=True)
            time.sleep(60)

