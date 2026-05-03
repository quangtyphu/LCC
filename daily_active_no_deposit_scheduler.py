"""
Từ 23:00 đến cuối ngày (giờ máy chủ): mỗi 60 giây gọi API /api/users/active-no-deposit-today
và thử đưa user vào hàng chờ nạp (enqueue; trùng lặp vẫn bị chặn bởi can_create_deposit_order).
"""

import time
from datetime import datetime

import requests

from constants import load_config
from auto_deposit_on_out_of_money import (
    can_create_deposit_order,
    enqueue_deposit_order,
    is_in_v2_v3,
)

API_BASE = "http://127.0.0.1:3000"


def _normalize_username(item) -> str:
    if isinstance(item, dict):
        return str(item.get("username") or item.get("user") or item.get("id") or "").strip()
    return str(item).strip()


def _fetch_cms_usernames() -> set[str] | None:
    """
    Usernames hiện có trên CMS (GET /api/users).
    Trả None nếu gọi lỗi — khi đó không lọc để tránh loại hết danh sách.
    """
    try:
        r = requests.get(f"{API_BASE}/api/users", timeout=8)
        if r.status_code != 200:
            return None
        rows = r.json()
        if not isinstance(rows, list):
            return None
        out: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            u = _normalize_username(row)
            if u:
                out.add(u)
        return out
    except Exception:
        return None


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

        cms_names = _fetch_cms_usernames()
        if cms_names is not None:
            before = len(users)
            users = [u for u in users if u in cms_names]
            dropped = before - len(users)
            if dropped:
                print(
                    f"[NO-DEPOSIT] Đã bỏ {dropped} user không còn trong GET /api/users "
                    f"(tránh nick đã xóa khỏi CMS nhưng vẫn nằm trong active-no-deposit-today).",
                    flush=True,
                )
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

    print(
        f"[NO-DEPOSIT] {datetime.now().strftime('%H:%M')} — {len(users)} user chưa nạp (API), thử enqueue...",
        flush=True,
    )

    for user in users:
        if not user:
            continue

        if is_in_v2_v3(user, config):
            if config.get("AUTO_DEPOSIT_V2_V3", 0) != 1:
                continue
            if not can_create_deposit_order(user):
                continue
            enqueue_deposit_order(
                user,
                "active-no-deposit-today (≥23h): V2/V3/PRIORITY — API /api/users/active-no-deposit-today",
            )
        else:
            if config.get("AUTO_DEPOSIT_OUTSIDE_V2_V3", 0) != 1:
                continue
            if not can_create_deposit_order(user):
                continue
            enqueue_deposit_order(
                user,
                "active-no-deposit-today (≥23h): outside — API /api/users/active-no-deposit-today",
            )


def auto_active_no_deposit_scheduler():
    """
    Vòng lặp 60s. Khi giờ máy >= 23:00 (cùng ngày dương lịch), mỗi vòng gọi deposit_active_no_deposit_users.
    """
    print("[NO-DEPOSIT] 🕐 Scheduler: từ 23h mỗi 60s gọi API user chưa nạp trong ngày", flush=True)

    while True:
        try:
            now = datetime.now()
            if now.hour >= 23:
                deposit_active_no_deposit_users()

            time.sleep(60)

        except Exception as e:
            print(f"[NO-DEPOSIT] ❌ Lỗi scheduler: {e}", flush=True)
            time.sleep(60)

