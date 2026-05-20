# -*- coding: utf-8 -*-
"""Tổng cược ngày — cột accounts.daily_bet_total (+ daily_bet_day)."""

from __future__ import annotations

from typing import Any

from xoso66_accounts_db import get_daily_bet_totals, record_daily_bet  # noqa: F401

__all__ = [
    "ensure_daily_bet_table",
    "record_daily_bet",
    "get_daily_bet_totals",
    "fetch_daily_bets_from_api",
    "resolve_daily_bets",
]

get_daily_bets = get_daily_bet_totals


def ensure_daily_bet_table() -> None:
    """Giữ tương thích cũ — migration đã gộp vào accounts."""
    from xoso66_accounts_db import init_db

    init_db()


def fetch_daily_bets_from_api(
    usernames: list[str],
    api_base: str,
) -> dict[str, float]:
    """Tùy chọn: đọc từ CMS LC79 /api/bet-totals (username → total_day)."""
    import requests

    base = str(api_base or "").rstrip("/")
    if not base:
        return {u: 0.0 for u in usernames}
    res = {u: 0.0 for u in usernames}
    try:
        r = requests.get(
            f"{base}/api/bet-totals",
            params={"page": 1, "limit": 10000},
            timeout=8,
        )
        if r.status_code != 200:
            return res
        data = r.json()
        items = data.get("data") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return res
        for item in items:
            u = str(item.get("username") or item.get("user") or "").strip()
            if u and u in res:
                total_val = (
                    item.get("total_day")
                    or item.get("totalBet")
                    or item.get("total")
                    or item.get("today_bet")
                    or item.get("todayBet")
                    or 0
                )
                res[u] = float(total_val or 0)
    except Exception:
        pass
    return res


def resolve_daily_bets(
    rows: list[dict[str, Any]],
    cfg: dict,
) -> dict[str, float]:
    """account_id → tổng cược ngày (DB + optional API CMS)."""
    ids = [str(r["id"]) for r in rows if r.get("id")]
    local = get_daily_bet_totals(ids)
    ab = cfg if isinstance(cfg, dict) and "bet_totals_api" in cfg else {}
    if not ab and isinstance(cfg.get("auto_bet"), dict):
        ab = cfg["auto_bet"]
    api_base = str(ab.get("bet_totals_api") or "").strip()
    if api_base:
        unames = [str(r.get("username") or "") for r in rows]
        api_map = fetch_daily_bets_from_api(unames, api_base)
        for r in rows:
            aid = str(r["id"])
            u = str(r.get("username") or "")
            local[aid] = max(local.get(aid, 0), api_map.get(u, 0))
    return local
