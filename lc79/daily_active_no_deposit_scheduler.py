"""
Từ giờ START (config ACTIVE_NO_DEPOSIT_SCHEDULER.START, mặc định 23:00) đến cuối ngày
(giờ máy chủ): mỗi INTERVAL_SECONDS (mặc định 60) gọi API /api/users/active-no-deposit-today
và thử đưa user vào hàng chờ nạp (enqueue; trùng lặp vẫn bị chặn bởi can_create_deposit_order).

Bật/tắt: ACTIVE_NO_DEPOSIT_SCHEDULER.ENABLED (1 = bật, mặc định khi thiếu khối config).
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


def _parse_hhmm(s: str, default_h: int, default_m: int) -> tuple[int, int]:
    s = (s or "").strip()
    if not s:
        return default_h, default_m
    parts = s.split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        return max(0, min(23, h)), max(0, min(59, m))
    except (ValueError, IndexError):
        return default_h, default_m


def _active_no_deposit_scheduler_settings(config: dict | None) -> tuple[bool, int, int, int]:
    """
    (enabled, start_hour, start_minute, interval_seconds).
    Thiếu ACTIVE_NO_DEPOSIT_SCHEDULER: coi như ENABLED=1, START=23:00, INTERVAL_SECONDS=60.
    """
    default = (True, 23, 0, 60)
    if not config:
        return default
    block = config.get("ACTIVE_NO_DEPOSIT_SCHEDULER")
    if not isinstance(block, dict):
        return default
    try:
        enabled = int(block.get("ENABLED", 1)) == 1
    except (TypeError, ValueError):
        enabled = True
    sh, sm = _parse_hhmm(str(block.get("START", "23:00")).strip(), 23, 0)
    try:
        interval = int(block.get("INTERVAL_SECONDS", 60))
    except (TypeError, ValueError):
        interval = 60
    interval = max(1, interval)
    return enabled, sh, sm, interval


def _in_active_no_deposit_time_window(now: datetime, start_h: int, start_m: int) -> bool:
    start_minutes = start_h * 60 + start_m
    now_minutes = now.hour * 60 + now.minute
    return now_minutes >= start_minutes


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

    enabled, sh, sm, _iv = _active_no_deposit_scheduler_settings(config)
    if not enabled:
        return
    start_label = f"{sh:02d}:{sm:02d}"

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
                f"active-no-deposit-today (≥{start_label}): V2/V3/PRIORITY — API /api/users/active-no-deposit-today",
            )
        else:
            if config.get("AUTO_DEPOSIT_OUTSIDE_V2_V3", 0) != 1:
                continue
            if not can_create_deposit_order(user):
                continue
            enqueue_deposit_order(
                user,
                f"active-no-deposit-today (≥{start_label}): outside — API /api/users/active-no-deposit-today",
            )


def auto_active_no_deposit_scheduler():
    """
    Vòng lặp theo INTERVAL_SECONDS trong config. Khi giờ máy >= START (cùng ngày dương lịch),
    mỗi vòng gọi deposit_active_no_deposit_users nếu ENABLED=1.
    """
    cfg0 = load_config() or {}
    _en0, sh0, sm0, iv0 = _active_no_deposit_scheduler_settings(cfg0)
    print(
        f"[NO-DEPOSIT] 🕐 Scheduler: ENABLED={int(_en0)} từ {sh0:02d}:{sm0:02d} "
        f"mỗi {iv0}s gọi API user chưa nạp trong ngày (ACTIVE_NO_DEPOSIT_SCHEDULER)",
        flush=True,
    )

    while True:
        try:
            config = load_config() or {}
            enabled, sh, sm, interval = _active_no_deposit_scheduler_settings(config)
            now = datetime.now()
            if enabled and _in_active_no_deposit_time_window(now, sh, sm):
                deposit_active_no_deposit_users()

            time.sleep(interval)

        except Exception as e:
            print(f"[NO-DEPOSIT] ❌ Lỗi scheduler: {e}", flush=True)
            _cfg = load_config() or {}
            _, _, _, _iv = _active_no_deposit_scheduler_settings(_cfg)
            time.sleep(_iv)

