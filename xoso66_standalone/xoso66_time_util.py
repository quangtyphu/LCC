# -*- coding: utf-8 -*-
"""Múi giờ VN — tổng cược ngày, reset theo ngày CMS/LC79."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def today_vn() -> date:
    return datetime.now(VN_TZ).date()


def today_vn_str() -> str:
    """YYYY-MM-DD theo Asia/Ho_Chi_Minh (không phụ thuộc TZ máy chủ)."""
    return today_vn().isoformat()


def now_vn() -> datetime:
    return datetime.now(VN_TZ)


def vn_hour_in_quiet_range(hour: int, start_hour: int, end_hour: int) -> bool:
    """
    Khung [start_hour, end_hour) giờ VN (0–23).
    start == end → tắt. Qua nửa đêm: VD 22→06 = từ 22h đến trước 6h.
    """
    start_hour = int(start_hour) % 24
    end_hour = int(end_hour) % 24
    hour = int(hour) % 24
    if start_hour == end_hour:
        return False
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def auto_bet_quiet_hours_window(acfg: dict) -> tuple[bool, int, int]:
    """
    Đọc auto_bet.quiet_hours {enabled, start_hour, end_hour}
    hoặc quiet_hours_enabled / quiet_hours_start_hour / quiet_hours_end_hour.
    """
    qh = acfg.get("quiet_hours")
    if isinstance(qh, dict) and qh.get("enabled") is not None:
        if not qh.get("enabled"):
            return False, 0, 0
        start = int(qh.get("start_hour", qh.get("hour_start", 2)))
        end = int(qh.get("end_hour", qh.get("hour_end", 8)))
        return True, start, end
    if not acfg.get("quiet_hours_enabled"):
        return False, 0, 0
    start = int(acfg.get("quiet_hours_start_hour", 2))
    end = int(acfg.get("quiet_hours_end_hour", 8))
    return True, start, end


def auto_bet_in_quiet_hours(acfg: dict, *, now: datetime | None = None) -> bool:
    """True nếu đang trong khung không đặt cược (giờ VN)."""
    enabled, start, end = auto_bet_quiet_hours_window(acfg)
    if not enabled:
        return False
    if now is None:
        now = now_vn()
    return vn_hour_in_quiet_range(now.hour, start, end)


def format_auto_bet_quiet_hours(acfg: dict) -> str:
    enabled, start, end = auto_bet_quiet_hours_window(acfg)
    if not enabled:
        return ""
    return f"{start:02d}:00–{end:02d}:00 VN"
