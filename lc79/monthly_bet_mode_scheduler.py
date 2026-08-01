"""
Chế độ Cược tháng: mỗi 60 giây (khi ENABLED=1) gọi CMS
GET /api/users/lc79-playing-or-out + merge total_month từ /api/bet-totals,
lọc Đang Chơi / Hết Tiền, sắp xếp total_month giảm dần,
lấy user có THRESHOLD_MIN <= total_month < THRESHOLD (sàn/trần) → ghi PRIORITY_USERS_V2.

Config MONTHLY_BET_MODE:
  ENABLED: 0 tắt, 1 bật (chiaTien_Acc ép strategy 13)
  THRESHOLD: trần cược tháng (exclusive — user đạt đúng ngưỡng không được chọn)
  THRESHOLD_MIN: sàn cược tháng (vd. 15_000_000 = 15tr)
  USER_COUNT: 0 = lấy tất cả user trong khoảng; >0 = giới hạn tối đa

Khi mọi user trong PRIORITY_USERS_V2 có total_month > THRESHOLD → ENABLED=0, xóa V2.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List

import requests

from constants import load_config, save_config

API_BASE = "http://127.0.0.1:3000"
STATUS_FETCH_URL = f"{API_BASE}/api/users/lc79-playing-or-out"
BET_TOTALS_URL = f"{API_BASE}/api/bet-totals"

_ALLOWED_STATUSES = frozenset({"Đang Chơi", "Hết Tiền"})
_config_lock = threading.Lock()


def monthly_bet_mode_forces_strategy(cfg: dict) -> bool:
    """ENABLED=1 trong MONTHLY_BET_MODE → chiaTien_Acc dùng strategy 13 (PRIORITY → V2)."""
    block = cfg.get("MONTHLY_BET_MODE")
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


def _total_month_from_row(row: dict) -> int:
    return _to_int(
        row.get("total_month")
        or row.get("totalMonth")
        or row.get("month_bet")
        or row.get("monthBet")
        or 0,
        0,
    )


def _fetch_bet_totals_rows() -> List[dict]:
    try:
        r = requests.get(BET_TOTALS_URL, params={"page": 1, "limit": 10000}, timeout=12)
        if r.status_code != 200:
            return []
        payload = r.json()
        items = payload.get("data") if isinstance(payload, dict) else payload
        return items if isinstance(items, list) else []
    except Exception:
        return []


def _month_by_username_from_bet_totals(bet_rows: List[dict]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for row in bet_rows:
        if not isinstance(row, dict):
            continue
        u = _username_from_row(row)
        if not u:
            continue
        tm = _total_month_from_row(row)
        if u not in out or tm > out[u]:
            out[u] = tm
    return out


def _merge_month_into_status_rows(
    status_rows: List[dict],
    bet_rows: List[dict],
) -> List[dict]:
    """Gắn total_month từ bet-totals vào row status (giữ nguyên status/username)."""
    month_by_name = _month_by_username_from_bet_totals(bet_rows)
    merged: List[dict] = []
    for row in status_rows:
        if not isinstance(row, dict):
            continue
        u = _username_from_row(row)
        new_row = dict(row)
        if u and u in month_by_name:
            new_row["total_month"] = month_by_name[u]
        merged.append(new_row)
    return merged


def _total_month_by_username(rows: List[dict]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for row in rows:
        u = _username_from_row(row)
        if not u:
            continue
        tm = _total_month_from_row(row)
        if u not in out or tm > out[u]:
            out[u] = tm
    return out


def compute_monthly_v2_users(
    rows: List[dict],
    month_max: int,
    month_min: int,
    user_count: int,
) -> List[str]:
    """Lọc status + month_min <= total_month < month_max, sort total_month giảm dần; user_count<=0 = không giới hạn."""
    lo = max(0, int(month_min))
    hi = max(0, int(month_max))
    acc: Dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "").strip()
        if status not in _ALLOWED_STATUSES:
            continue
        u = _username_from_row(row)
        if not u:
            continue
        tm = _total_month_from_row(row)
        if tm < lo or tm >= hi:
            continue
        if u not in acc or tm > acc[u]:
            acc[u] = tm
    ordered = sorted(acc.items(), key=lambda x: (-x[1], x[0]))
    usernames = [u for u, _ in ordered]
    if user_count > 0:
        return usernames[:user_count]
    return usernames


def _normalize_v2_slots(lst: List[Any], nslots: int) -> List[str]:
    out: List[str] = []
    for i in range(nslots):
        if i < len(lst):
            out.append(str(lst[i] or "").strip())
        else:
            out.append("")
    return out


def _v2_usernames_from_cfg(cfg: dict) -> List[str]:
    lst = cfg.get("PRIORITY_USERS_V2")
    if not isinstance(lst, list):
        return []
    return [str(u or "").strip() for u in lst if str(u or "").strip()]


def _all_v2_users_exceeded_threshold(
    rows: List[dict],
    v2_users: List[str],
    threshold: int,
) -> bool:
    """True khi mọi user V2 có total_month > threshold (thiếu trong API → chưa vượt)."""
    if not v2_users:
        return False
    by_name = _total_month_by_username(rows)
    for u in v2_users:
        tm = by_name.get(u)
        if tm is None or tm <= threshold:
            return False
    return True


def _disable_monthly_bet_mode(cfg: dict) -> bool:
    """Tắt ENABLED, xóa PRIORITY_USERS_V2 (giữ số slot), lưu config."""
    block = cfg.get("MONTHLY_BET_MODE")
    if not isinstance(block, dict):
        block = {}
        cfg["MONTHLY_BET_MODE"] = block
    block["ENABLED"] = 0

    v2_old = cfg.get("PRIORITY_USERS_V2")
    if not isinstance(v2_old, list):
        v2_old = []
    slots = len(v2_old) if v2_old else 0
    if slots > 0:
        cfg["PRIORITY_USERS_V2"] = [""] * slots
    elif isinstance(cfg.get("PRIORITY_USERS_V2"), list):
        cfg["PRIORITY_USERS_V2"] = []

    return save_config(cfg)


def monthly_bet_mode_tick() -> None:
    with _config_lock:
        cfg = load_config()
        if not cfg:
            return
        block = cfg.get("MONTHLY_BET_MODE")
        if not isinstance(block, dict):
            return
        try:
            enabled = int(block.get("ENABLED", 0))
        except (TypeError, ValueError):
            enabled = 0
        if enabled != 1:
            return

        month_max = _to_int(block.get("THRESHOLD", 0), 0)
        month_min = _to_int(block.get("THRESHOLD_MIN", 0), 0)
        user_count = _to_int(block.get("USER_COUNT", 0), 0)
        if month_max <= 0:
            print("[CUOC-THANG] ⚠️ THRESHOLD (trần) không hợp lệ, bỏ qua tick", flush=True)
            return

        try:
            r = requests.get(STATUS_FETCH_URL, timeout=12)
        except Exception as e:
            print(f"[CUOC-THANG] ⚠️ Lỗi gọi API status: {e}", flush=True)
            return
        if r.status_code != 200:
            print(f"[CUOC-THANG] ⚠️ API status {r.status_code}: {r.text[:240]}", flush=True)
            return
        try:
            payload = r.json()
        except Exception as e:
            print(f"[CUOC-THANG] ⚠️ Parse JSON status lỗi: {e}", flush=True)
            return

        status_rows = _parse_users_payload(payload)
        bet_rows = _fetch_bet_totals_rows()
        rows = _merge_month_into_status_rows(status_rows, bet_rows)

        v2_users = _v2_usernames_from_cfg(cfg)
        if v2_users and _all_v2_users_exceeded_threshold(rows, v2_users, month_max):
            by_name = _total_month_by_username(rows)
            summary = ", ".join(f"{u}({by_name.get(u, 0):,})" for u in v2_users)
            if _disable_monthly_bet_mode(cfg):
                print(
                    f"[CUOC-THANG] 🏁 Hoàn thành — mọi user V2 vượt {month_max:,}: {summary}. "
                    f"ENABLED=0, đã xóa PRIORITY_USERS_V2",
                    flush=True,
                )
            else:
                print("[CUOC-THANG] ❌ Không lưu được config.json khi tắt chế độ", flush=True)
            return

        selected = compute_monthly_v2_users(rows, month_max, month_min, user_count)

        v2_old = cfg.get("PRIORITY_USERS_V2")
        if not isinstance(v2_old, list):
            v2_old = []
        slots = max(len(v2_old), len(selected), user_count if user_count > 0 else 0)
        new_v2 = selected + [""] * (slots - len(selected))
        new_v2 = new_v2[:slots]

        if _normalize_v2_slots(v2_old, slots) == new_v2:
            return

        cfg["PRIORITY_USERS_V2"] = new_v2
        if save_config(cfg):
            lo = max(0, month_min)
            hi = max(0, month_max)
            cap_label = f"{len(selected)}/{user_count}" if user_count > 0 else str(len(selected))
            print(
                f"[CUOC-THANG] ✅ Đã cập nhật V2 ({cap_label} user, tháng [{lo:,} .. {hi:,})): "
                f"{', '.join(selected) or '(trống)'}",
                flush=True,
            )
        else:
            print("[CUOC-THANG] ❌ Không lưu được config.json", flush=True)


def monthly_bet_mode_scheduler_loop() -> None:
    print("[CUOC-THANG] Scheduler đã khởi động (60s/tick khi MONTHLY_BET_MODE.ENABLED=1)", flush=True)
    while True:
        try:
            monthly_bet_mode_tick()
        except Exception as e:
            print(f"[CUOC-THANG] ❌ Lỗi tick: {e}", flush=True)
            import traceback

            traceback.print_exc()
        time.sleep(60)


def start_monthly_bet_mode_scheduler() -> None:
    threading.Thread(target=monthly_bet_mode_scheduler_loop, daemon=True).start()
