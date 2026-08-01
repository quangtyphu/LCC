# -*- coding: utf-8 -*-
"""Cấu hình Game B chung (SEXY / bàn C06) — dùng bởi mọi portal."""

from __future__ import annotations

from typing import Any

DEFAULT_PLATFORM_ID = 1012
DEFAULT_CATEGORY_ID = 4
DEFAULT_TABLE_NAME = "C06"
DEFAULT_TABLE_ID = 1006


def vendor_table_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    v = cfg.get("vendor")
    if not isinstance(v, dict):
        v = {}
    auto_raw = v.get("auto_bet_enabled", v.get("auto_bet"))
    if isinstance(auto_raw, bool):
        auto_bet = auto_raw
    else:
        auto_bet = str(auto_raw or "").strip().lower() in ("1", "true", "yes", "on")
    return {
        "platform_id": int(v.get("platform_id") or DEFAULT_PLATFORM_ID),
        "category_id": int(v.get("category_id") or DEFAULT_CATEGORY_ID),
        "table_name": str(v.get("table_name") or DEFAULT_TABLE_NAME),
        "table_id": int(v.get("table_id") or DEFAULT_TABLE_ID),
        "auto_bet_enabled": auto_bet,
        "stake_min": int(v.get("stake_min") or 10),
        "stake_max": int(v.get("stake_max") or 20),
        "stake_unit": str(v.get("stake_unit") or "k"),
        "stake_step": int(v.get("stake_step") or 10),
        "bet_limit_id": int(v.get("bet_limit_id") or 851101),
        "max_bet_rounds": int(v.get("max_bet_rounds") or 0),
        "keepalive_maintain_sec": int(v.get("keepalive_maintain_sec") or 90),
        "keepalive_anti_idle_sec": int(v.get("keepalive_anti_idle_sec") or 300),
        "ws_wait_pre_enter_sec": float(v.get("ws_wait_pre_enter_sec") or 0),
        "ws_wait_in_table_sec": float(v.get("ws_wait_in_table_sec") or 60),
        "ws_wait_post_cdp_sec": float(v.get("ws_wait_post_cdp_sec") or 40),
        "ws_round_wait_sec": float(v.get("ws_round_wait_sec") or 120),
        "open_game_settle_ms": int(v.get("open_game_settle_ms") or 2500),
        "find_vendor_timeout_sec": float(v.get("find_vendor_timeout_sec") or 20),
        "enter_lobby_wait_sec": float(v.get("enter_lobby_wait_sec") or 15),
        "enter_lobby_settle_sec": float(v.get("enter_lobby_settle_sec") or 2),
        "enter_table_timeout_sec": float(v.get("enter_table_timeout_sec") or 45),
    }
