# time_windows.py — chọn khung giờ từ TIME_WINDOWS; overlay PAUSE chỉ khi PERIODIC_CHECK (ENABLED tắt)
from __future__ import annotations

from datetime import datetime
from typing import Any


def _parse_hhmm(raw: str):
    from datetime import datetime as dt

    return dt.strptime(raw, "%H:%M").time()


def _resolve_calendar_window(cfg: dict, now_t) -> dict:
    """
    Trả về item TIME_WINDOWS đang khớp (inclusive start, exclusive end).
    Hỗ trợ khoảng qua nửa đêm (start > end). Không khớp -> {}.
    """
    windows = cfg.get("TIME_WINDOWS") or []
    from datetime import datetime as dt

    for w in windows:
        s_raw, e_raw = w.get("start"), w.get("end")
        if not s_raw or not e_raw:
            continue
        try:
            s = dt.strptime(s_raw, "%H:%M").time()
            e = dt.strptime(e_raw, "%H:%M").time()
        except Exception:
            continue
        in_range = (s <= now_t < e) if s < e else (now_t >= s or now_t < e)
        if in_range:
            return w
    return {}


def get_active_window(cfg: dict) -> dict:
    """Khung giờ đang hiệu lực (VN), gồm overlay kéo cược sau 2h khi jackpot cao."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    now_dt = datetime.now(tz)
    base = _resolve_calendar_window(cfg, now_dt.time())
    try:
        from jackpot_night_extend import apply_pause_overlay_if_eligible

        return apply_pause_overlay_if_eligible(cfg, base, now_dt)
    except ImportError:
        return base
