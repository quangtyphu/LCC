"""
Chế độ Cược tuần: mỗi 60 giây (khi ENABLED=1) gọi CMS
GET /api/users/lc79-playing-or-out, lọc Đang Chơi / Hết Tiền, sắp xếp total_week giảm dần,
chỉ lấy user có THRESHOLD_MIN <= total_week <= THRESHOLD (trần/sàn), tối đa USER_COUNT → ghi PRIORITY_USERS_V2.

API Node.js nên trả JSON dạng:
  { "data": [ { "username": "...", "status": "Đang Chơi"|"Hết Tiền", "total_week": 19000000 }, ... ] }
hoặc list trực tiếp. Trường total_week có thể là totalWeek / week_bet / weekBet.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List

import requests

from constants import load_config, save_config

API_BASE = "http://127.0.0.1:3000"
FETCH_URL = f"{API_BASE}/api/users/lc79-playing-or-out"

_ALLOWED_STATUSES = frozenset({"Đang Chơi", "Hết Tiền"})
_config_lock = threading.Lock()


def weekly_bet_mode_forces_strategy(cfg: dict) -> bool:
    """ENABLED=1 trong WEEKLY_BET_MODE → chiaTien_Acc dùng strategy 6."""
    block = cfg.get("WEEKLY_BET_MODE")
    if not isinstance(block, dict):
        return False
    try:
        return int(block.get("ENABLED", 0)) == 1
    except (TypeError, ValueError):
        return False


def _to_int(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _parse_users_payload(data: Any) -> List[dict]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("data", "users", "items"):
            inner = data.get(key)
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
    return []


def _username_from_row(row: dict) -> str:
    u = row.get("username") or row.get("user") or row.get("name")
    return str(u).strip() if u else ""


def _total_week_from_row(row: dict) -> int:
    return _to_int(
        row.get("total_week")
        or row.get("totalWeek")
        or row.get("week_bet")
        or row.get("weekBet")
        or 0,
        0,
    )


def compute_weekly_v2_users(
    rows: List[dict],
    week_max: int,
    week_min: int,
    user_count: int,
) -> List[str]:
    """Lọc status + week_min <= total_week <= week_max, sort total_week giảm dần, tối đa user_count (unique)."""
    lo = max(0, int(week_min))
    hi = max(0, int(week_max))
    if lo > hi:
        lo, hi = hi, lo
    acc: Dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "").strip()
        if status not in _ALLOWED_STATUSES:
            continue
        u = _username_from_row(row)
        if not u:
            continue
        tw = _total_week_from_row(row)
        if tw < lo or tw > hi:
            continue
        if u not in acc or tw > acc[u]:
            acc[u] = tw
    ordered = sorted(acc.items(), key=lambda x: (-x[1], x[0]))
    return [u for u, _ in ordered[: max(0, user_count)]]


def _normalize_v2_slots(lst: List[Any], nslots: int) -> List[str]:
    out: List[str] = []
    for i in range(nslots):
        if i < len(lst):
            out.append(str(lst[i] or "").strip())
        else:
            out.append("")
    return out


def weekly_bet_mode_tick() -> None:
    with _config_lock:
        cfg = load_config()
        if not cfg:
            return
        block = cfg.get("WEEKLY_BET_MODE")
        if not isinstance(block, dict):
            return
        try:
            enabled = int(block.get("ENABLED", 0))
        except (TypeError, ValueError):
            enabled = 0
        if enabled != 1:
            return

        week_max = _to_int(block.get("THRESHOLD", 0), 0)
        week_min = _to_int(block.get("THRESHOLD_MIN", 0), 0)
        user_count = _to_int(block.get("USER_COUNT", 10), 10)
        if week_max <= 0 or user_count <= 0:
            print("[CUOC-TUAN] ⚠️ THRESHOLD (trần) hoặc USER_COUNT không hợp lệ, bỏ qua tick", flush=True)
            return

        try:
            r = requests.get(FETCH_URL, timeout=12)
        except Exception as e:
            print(f"[CUOC-TUAN] ⚠️ Lỗi gọi API: {e}", flush=True)
            return
        if r.status_code != 200:
            print(f"[CUOC-TUAN] ⚠️ API {r.status_code}: {r.text[:240]}", flush=True)
            return
        try:
            payload = r.json()
        except Exception as e:
            print(f"[CUOC-TUAN] ⚠️ Parse JSON lỗi: {e}", flush=True)
            return

        rows = _parse_users_payload(payload)
        selected = compute_weekly_v2_users(rows, week_max, week_min, user_count)

        v2_old = cfg.get("PRIORITY_USERS_V2")
        if not isinstance(v2_old, list):
            v2_old = []
        slots = max(len(v2_old), user_count)
        new_v2 = selected + [""] * (slots - len(selected))
        new_v2 = new_v2[:slots]

        if _normalize_v2_slots(v2_old, slots) == new_v2:
            return

        cfg["PRIORITY_USERS_V2"] = new_v2
        if save_config(cfg):
            lo = max(0, week_min)
            hi = max(0, week_max)
            if lo > hi:
                lo, hi = hi, lo
            print(
                f"[CUOC-TUAN] ✅ Đã cập nhật V2 ({len(selected)}/{user_count} user, tuần [{lo:,} .. {hi:,}]): "
                f"{', '.join(selected) or '(trống)'}",
                flush=True,
            )
        else:
            print("[CUOC-TUAN] ❌ Không lưu được config.json", flush=True)


def weekly_bet_mode_scheduler_loop() -> None:
    print("[CUOC-TUAN] Scheduler đã khởi động (60s/tick khi WEEKLY_BET_MODE.ENABLED=1)", flush=True)
    while True:
        try:
            weekly_bet_mode_tick()
        except Exception as e:
            print(f"[CUOC-TUAN] ❌ Lỗi tick: {e}", flush=True)
            import traceback

            traceback.print_exc()
        time.sleep(60)


def start_weekly_bet_mode_scheduler() -> None:
    threading.Thread(target=weekly_bet_mode_scheduler_loop, daemon=True).start()
