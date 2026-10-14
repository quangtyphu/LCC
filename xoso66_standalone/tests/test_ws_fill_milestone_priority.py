# -*- coding: utf-8 -*-

from xoso66_time_util import today_vn_str
from xoso66_ws_pool import sort_rows_ws_fill_priority


def _cfg(*, cap_vnd: int, ws_fill_priority: int = 2) -> dict:
    return {
        "daily_bet_cap_reset": {"value_vnd": 895_000},
        "auto_bet": {"daily_bet_cap_vnd": cap_vnd, "bet_step_vnd": 10_000},
        "game_worker": {"ws_fill_priority": ws_fill_priority},
    }


def _row(aid: str, daily: float, balance: float = 0) -> dict:
    return {
        "id": aid,
        "username": aid,
        "daily_bet_day": today_vn_str(),
        "daily_bet_total": daily,
        "balance": balance,
    }


def test_ws_fill_priority2_raised_cap_prefers_below_890k():
    cfg = _cfg(cap_vnd=2_695_000, ws_fill_priority=2)
    rows = [_row("high", 2_000_000), _row("low", 400_000)]
    ordered = sort_rows_ws_fill_priority(rows, cfg)
    assert [r["id"] for r in ordered] == ["low", "high"]


def test_ws_fill_priority2_raised_cap_900k_after_under_890k_tier():
    cfg = _cfg(cap_vnd=2_695_000, ws_fill_priority=2)
    rows = [_row("mid", 900_000), _row("low", 400_000), _row("high", 2_000_000)]
    ordered = sort_rows_ws_fill_priority(rows, cfg)
    assert [r["id"] for r in ordered] == ["low", "high", "mid"]


def test_ws_fill_priority2_at_895k_cap_unchanged_high_daily_first():
    cfg = _cfg(cap_vnd=895_000, ws_fill_priority=2)
    rows = [_row("high", 2_000_000), _row("low", 400_000)]
    ordered = sort_rows_ws_fill_priority(rows, cfg)
    assert [r["id"] for r in ordered] == ["high", "low"]
