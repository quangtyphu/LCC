# -*- coding: utf-8 -*-
"""Thời gian xác nhận nạp/rút.

Nạp: confirm_duration_sec = giây từ Đã Nạp → game Hoàn tất.
Rút: confirm_duration_sec = số lần poll thành công (create_time game là lúc gửi lệnh, không dùng đo giây).
"""

from __future__ import annotations

import time
from typing import Any


def game_item_end_ms(item: dict[str, Any] | None) -> int:
    """Thời điểm game ghi nhận Hoàn tất (create_time trên paymentorderlist)."""
    if not isinstance(item, dict):
        return 0
    raw_ms = item.get("create_time_ms")
    if raw_ms is not None:
        try:
            return int(raw_ms)
        except (TypeError, ValueError):
            pass
    from xoso66_payment_history_db import parse_payment_create_time_ms

    parsed = parse_payment_create_time_ms(str(item.get("create_time") or ""))
    return int(parsed) if parsed is not None else 0


def compute_confirm_duration_sec(start_ms: int, end_ms: int) -> int:
    """Giây từ start_ms → end_ms (game Hoàn tất). Trả 0 nếu thiếu mốc."""
    try:
        s = int(start_ms)
        e = int(end_ms)
    except (TypeError, ValueError):
        return 0
    if s <= 0 or e <= 0 or e < s:
        return 0
    return max(0, int(round((e - s) / 1000)))


def format_withdraw_poll_attempt_label(
    poll_attempt: int | float | None,
    poll_max: int | float | None = None,
) -> str:
    """Rút: số lần poll — vd. poll 3/20."""
    if poll_attempt is None:
        return "—"
    try:
        n = int(poll_attempt)
    except (TypeError, ValueError):
        return "—"
    if n <= 0:
        return "—"
    if poll_max is not None:
        try:
            mx = int(poll_max)
        except (TypeError, ValueError):
            mx = 0
        if mx > 0:
            return f"poll {n}/{mx}"
    return f"poll {n}"


def format_duration_label(total_sec: int | float | None) -> str:
    """Nạp: định dạng CMS 1p14s, 45s."""
    if total_sec is None:
        return "—"
    try:
        sec = int(total_sec)
    except (TypeError, ValueError):
        return "—"
    if sec <= 0:
        return "—"
    m, r = divmod(sec, 60)
    if m > 0:
        return f"{m}p{r}s"
    return f"{r}s"


def now_ms() -> int:
    return int(time.time() * 1000)
