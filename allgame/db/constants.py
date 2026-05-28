# -*- coding: utf-8 -*-
"""Trạng thái và transport chuẩn — đồng bộ CMS / LC79 / XOSO66."""

from __future__ import annotations

STATUS_NEW = "new"
STATUS_KHOA = "Khoá"
STATUS_DANG_CHOI = "Đang Chơi"
STATUS_HET_TIEN = "Hết Tiền"
STATUS_DU_NGAY = "Đủ ngày"
STATUS_TOKEN_LOI = "Token Lỗi"
STATUS_LOI = "Lỗi"

PLAYING_STATUSES = frozenset({STATUS_DANG_CHOI})

# Thứ tự sắp xếp mặc định (CMS / API đồng bộ)
STATUS_SORT_ORDER: dict[str, int] = {
    STATUS_DANG_CHOI: 1,
    STATUS_DU_NGAY: 2,
    STATUS_HET_TIEN: 3,
    STATUS_NEW: 4,
    STATUS_KHOA: 5,
    STATUS_TOKEN_LOI: 6,
}

TRANSPORT_CHROME = "chrome"
TRANSPORT_WS = "ws"

VENDOR_IDLE = "idle"
VENDOR_LOBBY_OK = "lobby_ok"
VENDOR_OPEN = "vendor_open"
VENDOR_AT_TABLE = "at_table"

ACCOUNT_COLUMNS = (
    "portal_id",
    "username",
    "password",
    "phone",
    "account_holder",
    "fund_password",
    "bank_code",
    "bank_name",
    "account_number",
    "proxy",
    "device",
    "balance",
    "total_deposit",
    "total_withdraw",
    "daily_bet_total",
    "daily_bet_day",
    "status",
    "transport",
    "chrome_browser_dir",
    "chrome_cdp_port",
    "vendor_state",
    "session_json",
    "portal_extra_json",
    "created_at",
    "updated_at",
)

SENSITIVE_KEYS = frozenset({"password", "fund_password"})
