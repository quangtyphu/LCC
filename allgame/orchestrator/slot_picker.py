# -*- coding: utf-8 -*-
"""Chọn acc tiếp theo cần mở Chrome — skeleton (ưu tiên daily thấp)."""

from __future__ import annotations

from typing import Any

from allgame.db.accounts_db import account_session_key, daily_bet_today_vnd
from allgame.orchestrator.session_registry import SessionRegistry


def pick_accounts_to_open(
    playing: list[dict[str, Any]],
    *,
    registry: SessionRegistry,
    limit: int,
    cfg: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []

    active = registry.keys()
    pool = [
        a
        for a in playing
        if account_session_key(str(a["portal_id"]), str(a["username"])) not in active
    ]
    if not pool:
        return []

    pool.sort(key=lambda r: daily_bet_today_vnd(r))
    return pool[:limit]
