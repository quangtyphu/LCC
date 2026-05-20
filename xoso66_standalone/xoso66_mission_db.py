# -*- coding: utf-8 -*-
"""
SQLite — snapshot nhiệm vụ từ mission/list (sync sau xoso66_daily_mission_check).

- Điểm danh mỗi ngày: mission_id 22, level_id 161 → daily_* + done_bet_money.
- MINI GAME 7 ngày: mission_id 17, level_id 114–120 (ngày 1–7) → mini_dayN_* + ngày hiện tại.

status: 0 = chưa đủ ĐK, 1 = được nhận, 2 = đã nhận.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from xoso66_accounts_db import DB_PATH, db_conn, init_db, set_daily_bet_from_mission_api
from xoso66_time_util import VN_TZ, today_vn_str

SIGN_ID_DAILY = 22
SIGN_ID_MINI = 17
DAILY_LEVEL_ID = 161
LEVEL_IDS_WEEK = tuple(range(114, 121))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_MISSION_DDL = """
CREATE TABLE IF NOT EXISTS account_missions (
    username TEXT PRIMARY KEY COLLATE NOCASE,
    account_id TEXT NOT NULL DEFAULT '',
    daily_mission_id INTEGER NOT NULL DEFAULT 22,
    daily_level_id INTEGER NOT NULL DEFAULT 161,
    daily_status INTEGER,
    daily_done_bet_money INTEGER NOT NULL DEFAULT 0,
    daily_progress INTEGER NOT NULL DEFAULT 0,
    daily_synced_day TEXT NOT NULL DEFAULT '',
    mini_mission_id INTEGER NOT NULL DEFAULT 17,
    mini_current_day INTEGER NOT NULL DEFAULT 0,
    mini_current_level_id INTEGER,
    mini_current_status INTEGER,
    mini_can_claim INTEGER NOT NULL DEFAULT 0,
    mini_current_done_bet INTEGER NOT NULL DEFAULT 0,
    mini_day1_status INTEGER,
    mini_day2_status INTEGER,
    mini_day3_status INTEGER,
    mini_day4_status INTEGER,
    mini_day5_status INTEGER,
    mini_day6_status INTEGER,
    mini_day7_status INTEGER,
    mini_day1_done_bet INTEGER NOT NULL DEFAULT 0,
    mini_day2_done_bet INTEGER NOT NULL DEFAULT 0,
    mini_day3_done_bet INTEGER NOT NULL DEFAULT 0,
    mini_day4_done_bet INTEGER NOT NULL DEFAULT 0,
    mini_day5_done_bet INTEGER NOT NULL DEFAULT 0,
    mini_day6_done_bet INTEGER NOT NULL DEFAULT 0,
    mini_day7_done_bet INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL
)
"""


def _migrate_mission_columns(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(account_missions)")}
    if "daily_synced_day" not in cols:
        conn.execute(
            "ALTER TABLE account_missions ADD COLUMN daily_synced_day TEXT NOT NULL DEFAULT ''"
        )


def init_mission_table(conn: sqlite3.Connection | None = None) -> None:
    if conn is not None:
        conn.execute(_MISSION_DDL)
        _migrate_mission_columns(conn)
        return
    with db_conn() as c:
        init_mission_table(c)


def infer_mini_game_state(week_levels: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Suy ra ngày hiện tại (1–7) từ status từng level_id.

    Ví dụ: 114=2 (đã nhận ngày 1), 115=1 → đang ngày 2, chưa nhận.
    """
    by_id: dict[int, dict[str, Any]] = {}
    for row in week_levels:
        lid = row.get("level_id") or row.get("id")
        if lid is not None:
            by_id[int(lid)] = row

    day_statuses: list[int | None] = []
    day_done_bets: list[int] = []
    for lid in LEVEL_IDS_WEEK:
        row = by_id.get(lid, {})
        st = row.get("status")
        day_statuses.append(int(st) if st is not None else None)
        day_done_bets.append(int(row.get("done_bet_money") or 0))

    current_day = 0
    current_level_id: int | None = None
    current_status: int | None = None
    can_claim = 0
    current_done_bet = 0

    for day_num, lid in enumerate(LEVEL_IDS_WEEK, start=1):
        st = day_statuses[day_num - 1]
        if st is None:
            continue
        if st in (0, 1):
            current_day = day_num
            current_level_id = lid
            current_status = st
            can_claim = 1 if st == 1 else 0
            current_done_bet = day_done_bets[day_num - 1]
            break
        if st == 2:
            current_day = day_num

    if current_status is None and day_statuses and all(s == 2 for s in day_statuses if s is not None):
        current_day = 7
        current_level_id = LEVEL_IDS_WEEK[-1]
        current_status = 2
        can_claim = 0
        current_done_bet = day_done_bets[-1]

    return {
        "current_day": current_day,
        "current_level_id": current_level_id,
        "current_status": current_status,
        "can_claim": can_claim,
        "current_done_bet": current_done_bet,
        "day_statuses": day_statuses,
        "day_done_bets": day_done_bets,
    }


def build_mission_snapshot(
    username: str,
    account_id: str,
    levels: list[dict[str, Any]],
) -> dict[str, Any]:
    """Từ collect_tracked_levels → dict lưu DB."""
    daily: dict[str, Any] = {}
    week: list[dict[str, Any]] = []
    for row in levels:
        lid = row.get("level_id") or row.get("id")
        mid = row.get("mission_id")
        if mid == SIGN_ID_DAILY or lid == DAILY_LEVEL_ID:
            daily = row
        elif mid == SIGN_ID_MINI or (lid in LEVEL_IDS_WEEK):
            week.append(row)

    mini = infer_mini_game_state(week)
    ds = mini["day_statuses"]
    dbets = mini["day_done_bets"]

    def _st(i: int) -> int | None:
        return ds[i] if i < len(ds) else None

    def _bet(i: int) -> int:
        return dbets[i] if i < len(dbets) else 0

    return {
        "username": username.strip(),
        "account_id": str(account_id),
        "daily_mission_id": SIGN_ID_DAILY,
        "daily_level_id": DAILY_LEVEL_ID,
        "daily_status": daily.get("status"),
        "daily_done_bet_money": int(daily.get("done_bet_money") or 0),
        "daily_progress": int(daily.get("progress") or 0),
        "daily_synced_day": today_vn_str(),
        "mini_mission_id": SIGN_ID_MINI,
        "mini_current_day": mini["current_day"],
        "mini_current_level_id": mini["current_level_id"],
        "mini_current_status": mini["current_status"],
        "mini_can_claim": mini["can_claim"],
        "mini_current_done_bet": mini["current_done_bet"],
        "mini_day1_status": _st(0),
        "mini_day2_status": _st(1),
        "mini_day3_status": _st(2),
        "mini_day4_status": _st(3),
        "mini_day5_status": _st(4),
        "mini_day6_status": _st(5),
        "mini_day7_status": _st(6),
        "mini_day1_done_bet": _bet(0),
        "mini_day2_done_bet": _bet(1),
        "mini_day3_done_bet": _bet(2),
        "mini_day4_done_bet": _bet(3),
        "mini_day5_done_bet": _bet(4),
        "mini_day6_done_bet": _bet(5),
        "mini_day7_done_bet": _bet(6),
        "synced_at": _now_iso(),
    }


def upsert_mission_snapshot(
    username: str,
    account_id: str,
    levels: list[dict[str, Any]],
) -> dict[str, Any]:
    init_db()
    snap = build_mission_snapshot(username, account_id, levels)
    with db_conn() as conn:
        init_mission_table(conn)
        conn.execute(
            """
            INSERT INTO account_missions (
                username, account_id,
                daily_mission_id, daily_level_id, daily_status,
                daily_done_bet_money, daily_progress, daily_synced_day,
                mini_mission_id, mini_current_day, mini_current_level_id,
                mini_current_status, mini_can_claim, mini_current_done_bet,
                mini_day1_status, mini_day2_status, mini_day3_status,
                mini_day4_status, mini_day5_status, mini_day6_status, mini_day7_status,
                mini_day1_done_bet, mini_day2_done_bet, mini_day3_done_bet,
                mini_day4_done_bet, mini_day5_done_bet, mini_day6_done_bet, mini_day7_done_bet,
                synced_at
            ) VALUES (
                ?, ?,
                ?, ?, ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?,
                ?
            )
            ON CONFLICT(username) DO UPDATE SET
                account_id=excluded.account_id,
                daily_status=excluded.daily_status,
                daily_done_bet_money=excluded.daily_done_bet_money,
                daily_progress=excluded.daily_progress,
                daily_synced_day=excluded.daily_synced_day,
                mini_current_day=excluded.mini_current_day,
                mini_current_level_id=excluded.mini_current_level_id,
                mini_current_status=excluded.mini_current_status,
                mini_can_claim=excluded.mini_can_claim,
                mini_current_done_bet=excluded.mini_current_done_bet,
                mini_day1_status=excluded.mini_day1_status,
                mini_day2_status=excluded.mini_day2_status,
                mini_day3_status=excluded.mini_day3_status,
                mini_day4_status=excluded.mini_day4_status,
                mini_day5_status=excluded.mini_day5_status,
                mini_day6_status=excluded.mini_day6_status,
                mini_day7_status=excluded.mini_day7_status,
                mini_day1_done_bet=excluded.mini_day1_done_bet,
                mini_day2_done_bet=excluded.mini_day2_done_bet,
                mini_day3_done_bet=excluded.mini_day3_done_bet,
                mini_day4_done_bet=excluded.mini_day4_done_bet,
                mini_day5_done_bet=excluded.mini_day5_done_bet,
                mini_day6_done_bet=excluded.mini_day6_done_bet,
                mini_day7_done_bet=excluded.mini_day7_done_bet,
                synced_at=excluded.synced_at
            """,
            (
                snap["username"],
                snap["account_id"],
                snap["daily_mission_id"],
                snap["daily_level_id"],
                snap["daily_status"],
                snap["daily_done_bet_money"],
                snap["daily_progress"],
                snap["daily_synced_day"],
                snap["mini_mission_id"],
                snap["mini_current_day"],
                snap["mini_current_level_id"],
                snap["mini_current_status"],
                snap["mini_can_claim"],
                snap["mini_current_done_bet"],
                snap["mini_day1_status"],
                snap["mini_day2_status"],
                snap["mini_day3_status"],
                snap["mini_day4_status"],
                snap["mini_day5_status"],
                snap["mini_day6_status"],
                snap["mini_day7_status"],
                snap["mini_day1_done_bet"],
                snap["mini_day2_done_bet"],
                snap["mini_day3_done_bet"],
                snap["mini_day4_done_bet"],
                snap["mini_day5_done_bet"],
                snap["mini_day6_done_bet"],
                snap["mini_day7_done_bet"],
                snap["synced_at"],
            ),
        )
    return snap


def force_daily_done_bet_to_account_total(account_id: str) -> int:
    """
    Server cập nhật done_bet_money (161) chậm: ghi local daily_done_bet_money
    = accounts.daily_bet_total (tổng cược ngày hôm nay).
    """
    from xoso66_accounts_db import daily_bet_today_vnd, get_account

    row = get_account(str(account_id).strip()) or {}
    total = int(daily_bet_today_vnd(row))
    username = str(row.get("username") or account_id).strip()
    if not username:
        return total
    init_db()
    now = _now_iso()
    today = today_vn_str()
    with db_conn() as conn:
        init_mission_table(conn)
        conn.execute(
            """
            UPDATE account_missions
            SET daily_done_bet_money = ?, daily_synced_day = ?, synced_at = ?
            WHERE account_id = ? OR username = ? COLLATE NOCASE
            """,
            (total, today, now, str(account_id).strip(), username),
        )
    return total


def persist_mission_state(
    username: str,
    account_id: str,
    levels: list[dict[str, Any]],
    *,
    phase: str = "list",
) -> dict[str, Any]:
    """
    Lưu account_missions + ghi đè accounts.daily_bet_total từ done_bet_money (161).
    Gọi sau mission/list và sau nhận thưởng.
    """
    snap = upsert_mission_snapshot(username, account_id, levels)
    daily_bet = int(snap.get("daily_done_bet_money") or 0)
    set_daily_bet_from_mission_api(account_id, daily_bet)
    snap["db_path"] = str(DB_PATH.resolve())
    snap["save_phase"] = phase
    snap["accounts_daily_bet_total"] = daily_bet
    return snap


def format_db_save_line(snap: dict[str, Any]) -> str:
    phase = snap.get("save_phase") or "list"
    return (
        f"DB [{phase}] {snap.get('db_path')} | "
        f"account_missions OK | accounts.daily_bet_total={int(snap.get('accounts_daily_bet_total') or 0):,} "
        f"(từ 161 done_bet={int(snap.get('daily_done_bet_money') or 0):,})"
    )


def get_mission_snapshot(username: str) -> dict[str, Any] | None:
    init_db()
    u = str(username or "").strip()
    with db_conn() as conn:
        init_mission_table(conn)
        row = conn.execute(
            "SELECT * FROM account_missions WHERE username = ? COLLATE NOCASE",
            (u,),
        ).fetchone()
    return dict(row) if row else None


def list_mission_snapshots() -> list[dict[str, Any]]:
    init_db()
    with db_conn() as conn:
        init_mission_table(conn)
        rows = conn.execute(
            "SELECT * FROM account_missions ORDER BY username"
        ).fetchall()
    return [dict(r) for r in rows]


_DAILY_STATUS_LABEL = {
    0: "Chưa đủ",
    1: "Đã đủ",
    2: "Đã nhận",
}


def _daily_161_label(status: Any) -> str:
    if status is None:
        return "—"
    try:
        return _DAILY_STATUS_LABEL.get(int(status), f"?({status})")
    except (TypeError, ValueError):
        return "—"


def _iso_to_vn_day(iso: str | None) -> str:
    """YYYY-MM-DD (VN) từ synced_at ISO."""
    if not iso:
        return ""
    try:
        s = str(iso).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(VN_TZ).date().isoformat()
    except (TypeError, ValueError):
        return ""


def _daily_161_synced_today(snapshot: dict[str, Any]) -> bool:
    """161 chỉ hợp lệ trong ngày — sang ngày mới coi như chưa có (chờ sync lại)."""
    day = str(snapshot.get("daily_synced_day") or "").strip()
    if day:
        return day == today_vn_str()
    # DB cũ chưa có cột: dùng ngày synced_at (có thể lệch nếu chỉ sync MINI hôm nay)
    legacy = _iso_to_vn_day(snapshot.get("synced_at"))
    return bool(legacy) and legacy == today_vn_str()


def _daily_161_effective(snapshot: dict[str, Any]) -> tuple[Any, int]:
    if not _daily_161_synced_today(snapshot):
        return None, 0
    st = snapshot.get("daily_status")
    done_bet = int(snapshot.get("daily_done_bet_money") or 0)
    return st, done_bet


def _mini_day_badge(day: int, status: Any) -> dict[str, Any]:
    """Một ngày trong chuỗi 7 ngày MINI GAME."""
    if status is None:
        return {"day": day, "status": None, "label": "—", "css": "mini-none"}
    st = int(status)
    if st == 2:
        return {"day": day, "status": st, "label": "✓", "css": "mini-done"}
    if st == 1:
        return {"day": day, "status": st, "label": "!", "css": "mini-claim"}
    return {"day": day, "status": st, "label": "○", "css": "mini-pending"}


def mission_cms_fields(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """
    Trường gắn vào /api/accounts cho CMS:
    - daily_161_* : nhiệm vụ điểm danh mỗi ngày (level 161)
    - mini_week_* : MINI GAME 7 ngày
    """
    if not snapshot:
        return {
            "mission_synced_at": None,
            "daily_161_status": None,
            "daily_161_label": "—",
            "daily_161_done_bet": 0,
            "mini_current_day": 0,
            "mini_current_label": "—",
            "mini_week_days": [],
        }

    st, done_bet = _daily_161_effective(snapshot)
    current_day = int(snapshot.get("mini_current_day") or 0)
    day_statuses = [
        snapshot.get(f"mini_day{i}_status") for i in range(1, 8)
    ]
    badges = [_mini_day_badge(i, day_statuses[i - 1]) for i in range(1, 8)]

    if current_day <= 0:
        mini_label = "—"
    elif all(s == 2 for s in day_statuses if s is not None) and all(
        s is not None for s in day_statuses
    ):
        mini_label = "Đủ 7 ngày"
    else:
        cur_st = snapshot.get("mini_current_status")
        if cur_st == 1:
            mini_label = f"Ngày {current_day} · được nhận"
        elif cur_st == 0:
            mini_label = f"Ngày {current_day} · chưa đủ"
        elif cur_st == 2 and current_day < 7:
            mini_label = f"Ngày {current_day + 1}"
        else:
            mini_label = f"Ngày {current_day}"

    synced_day = str(snapshot.get("daily_synced_day") or "").strip() or _iso_to_vn_day(
        snapshot.get("synced_at")
    )
    return {
        "mission_synced_at": snapshot.get("synced_at"),
        "daily_161_synced_day": synced_day or None,
        "daily_161_is_today": _daily_161_synced_today(snapshot),
        "daily_161_status": int(st) if st is not None else None,
        "daily_161_label": _daily_161_label(st),
        "daily_161_done_bet": done_bet,
        "mini_current_day": current_day,
        "mini_current_label": mini_label,
        "mini_week_days": badges,
    }


def mission_index_by_account() -> dict[str, dict[str, Any]]:
    """username (lower) và account_id → snapshot."""
    out: dict[str, dict[str, Any]] = {}
    for snap in list_mission_snapshots():
        u = str(snap.get("username") or "").strip().lower()
        aid = str(snap.get("account_id") or "").strip()
        if u:
            out[f"u:{u}"] = snap
        if aid:
            out[f"id:{aid}"] = snap
    return out


def mission_snapshot_for_account(
    account_id: str,
    username: str,
    *,
    index: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    idx = index if index is not None else mission_index_by_account()
    u = str(username or "").strip().lower()
    aid = str(account_id or "").strip()
    if u and f"u:{u}" in idx:
        return idx[f"u:{u}"]
    if aid and f"id:{aid}" in idx:
        return idx[f"id:{aid}"]
    return None
