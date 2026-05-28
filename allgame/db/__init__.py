# -*- coding: utf-8 -*-
"""DB thống nhất cho mọi cổng game (portal)."""

from allgame.db.accounts_db import (
    STATUS_DANG_CHOI,
    STATUS_DU_NGAY,
    STATUS_HET_TIEN,
    STATUS_LOI,
    STATUS_NEW,
    STATUS_TOKEN_LOI,
    account_session_key,
    daily_bet_today_vnd,
    get_account,
    init_db,
    list_accounts,
    list_accounts_by_status,
    list_playing_accounts,
    record_daily_bet,
    set_account_status,
    upsert_account,
)
from allgame.db.portals_db import get_portal, init_portals, list_portals, upsert_portal

__all__ = [
    "STATUS_DANG_CHOI",
    "STATUS_DU_NGAY",
    "STATUS_HET_TIEN",
    "STATUS_LOI",
    "STATUS_NEW",
    "STATUS_TOKEN_LOI",
    "account_session_key",
    "daily_bet_today_vnd",
    "get_account",
    "get_portal",
    "init_db",
    "init_portals",
    "list_accounts",
    "list_accounts_by_status",
    "list_playing_accounts",
    "list_portals",
    "record_daily_bet",
    "set_account_status",
    "upsert_account",
    "upsert_portal",
]
