"""
Scheduler nạp tiền cho user Hết Tiền có streak >= min trong ngày.
Luồng ngoài V2/V3: sau 20s — ưu tiên streak >= min, sau đó mới MAX_ACTIVE_USERS_OUTSIDE_V2_V3.
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
    _get_active_window,
    auto_deposit_for_user,
    outside_decision_try_skip,
    outside_decision_done,
)

API_BASE = "http://127.0.0.1:3000"
HET_TIEN_CHECK_DELAY_SECONDS = 20

_het_tien_slot_lock = threading.Lock()
_het_tien_slot_users: set = set()


def het_tien_slot_try_acquire(user: str) -> bool:
    """
    Chỉ một luồng xử lý nạp/hết tiền cho mỗi user (tránh poll + watcher + delayed trùng 2 lần).
    """
    u = (user or "").strip()
    if not u:
        return False
    with _het_tien_slot_lock:
        if u in _het_tien_slot_users:
            return False
        _het_tien_slot_users.add(u)
        return True


def het_tien_slot_release(user: str) -> None:
    u = (user or "").strip()
    if not u:
        return
    with _het_tien_slot_lock:
        _het_tien_slot_users.discard(u)


def run_het_tien_deposit_decision(user: str) -> None:
    """
    Quyết định nạp sau khi đã chờ đủ thời gian (gọi trực tiếp sau 20s từ delayed check,
    hoặc từ schedule sau sleep 20s).

    - V2/V3: auto_deposit_for_user (không qua streak).
    - Ngoài V2/V3: nếu streak >= HET_TIEN_STREAK_MIN → nạp user đó; không thì áp dụng
      MAX_ACTIVE_USERS_OUTSIDE_V2_V3 theo thứ tự API (prioritize_outside_trigger=False).
    """
    config = load_config()
    if not config:
        return
    w = _get_active_window(config)
    if w.get("PAUSE"):
        print(
            f"[SKIP] {user} nạp tự động: PAUSE ({w.get('start', 'N/A')}-{w.get('end', 'N/A')})",
            flush=True,
        )
        return

    if is_in_v2_v3(user, config):
        auto_deposit_for_user(user)
        return

    if config.get("AUTO_DEPOSIT_OUTSIDE_V2_V3", 0) != 1:
        return
    u = (user or "").strip()
    if not u:
        return
    if outside_decision_try_skip(u):
        return
    try:
        if _check_and_deposit_on_het_tien_if_streak_impl(user):
            return
        auto_deposit_for_user(user, prioritize_outside_trigger=False, from_decision_chain=True)
    finally:
        outside_decision_done(u)


def schedule_het_tien_deposit_after_delay(user: str) -> None:
    """
    Sau khi chuyển Hết Tiền:
    - V2/V3 + AUTO_DEPOSIT_V2_V3: xử lý ngay (giữ cũ, không thread 20s).
    - Ngoài V2/V3: chờ 20s rồi run_het_tien_deposit_decision (KQ/streak cập nhật chậm).
    """
    config = load_config()
    if not config:
        return
    if not het_tien_slot_try_acquire(user):
        return
    if is_in_v2_v3(user, config) and int(config.get("AUTO_DEPOSIT_V2_V3", 0) or 0) == 1:
        try:
            run_het_tien_deposit_decision(user)
        finally:
            het_tien_slot_release(user)
        return

    def _run():
        try:
            time.sleep(HET_TIEN_CHECK_DELAY_SECONDS)
            run_het_tien_deposit_decision(user)
        finally:
            het_tien_slot_release(user)

    threading.Thread(target=_run, daemon=True).start()


def check_and_deposit_on_het_tien_if_streak(user: str) -> None:
    """Alias: lên lịch sau 20s (ngoài V2/V3) hoặc xử lý ngay (V2/V3 + auto)."""
    schedule_het_tien_deposit_after_delay(user)


def _check_and_deposit_on_het_tien_if_streak_impl(user: str) -> bool:
    """
    Chỉ nhánh streak: user có trong API het-tien-streak với min streak → enqueue nạp.
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
        return False
    else:
        if config.get("AUTO_DEPOSIT_OUTSIDE_V2_V3", 0) != 1:
            return False
    if not can_create_deposit_order(user):
        return False
    enqueue_deposit_order(user)
    print(
        f"[STREAK] [{user}] Hết tiền + dây thắng/thua (>={min_streak}) → nạp (ưu tiên trước MAX_ACTIVE)",
        flush=True,
    )
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
