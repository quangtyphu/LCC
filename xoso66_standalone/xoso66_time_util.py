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
