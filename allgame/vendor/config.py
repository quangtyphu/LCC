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
    return {
        "platform_id": int(v.get("platform_id") or DEFAULT_PLATFORM_ID),
        "category_id": int(v.get("category_id") or DEFAULT_CATEGORY_ID),
        "table_name": str(v.get("table_name") or DEFAULT_TABLE_NAME),
        "table_id": int(v.get("table_id") or DEFAULT_TABLE_ID),
    }
