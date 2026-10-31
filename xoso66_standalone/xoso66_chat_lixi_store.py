# -*- coding: utf-8 -*-
"""SQLite — lịch sử nhận lì xì phòng chat (chat_lixi_claims)."""

from __future__ import annotations

import sqlite3
from typing import Any

from xoso66_accounts_db import db_conn
from xoso66_time_util import now_vn, today_vn_str

_DDL = """
CREATE TABLE IF NOT EXISTS chat_lixi_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    red_id INTEGER,
    money INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    code INTEGER,
    msg TEXT NOT NULL DEFAULT '',
    vn_hour TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


def init_chat_lixi_table(conn: sqlite3.Connection | None = None) -> None:
    if conn is not None:
        _init_conn(conn)
        return
    with db_conn() as c:
        _init_conn(c)


def _init_conn(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_lixi_created ON chat_lixi_claims(created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_lixi_vn_hour ON chat_lixi_claims(vn_hour)"
    )


def vn_hour_key(dt=None) -> str:
    """Slot giờ VN: YYYY-MM-DDTHH."""
    d = dt or now_vn()
    return d.strftime("%Y-%m-%dT%H")


def _created_at_str() -> str:
    return now_vn().strftime("%Y-%m-%d %H:%M:%S")


def insert_claim(
    *,
    account_id: str,
    username: str = "",
    red_id: int | None = None,
    money: int = 0,
    status: str,
    code: int | None = None,
    msg: str = "",
    vn_hour: str | None = None,
) -> int:
    init_chat_lixi_table()
    hour = vn_hour or vn_hour_key()
    with db_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO chat_lixi_claims
              (account_id, username, red_id, money, status, code, msg, vn_hour, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(account_id or "").strip(),
                str(username or "").strip(),
                int(red_id) if red_id is not None else None,
                int(money or 0),
                str(status or "").strip() or "error",
                int(code) if code is not None else None,
                str(msg or "")[:500],
                hour,
                _created_at_str(),
            ),
        )
        return int(cur.lastrowid or 0)


def list_claims(
    *,
    date: str | None = None,
    username: str | None = None,
    status: str | None = None,
    page: int = 1,
    limit: int = 50,
) -> dict[str, Any]:
    init_chat_lixi_table()
    page = max(1, int(page or 1))
    limit = max(1, min(200, int(limit or 50)))
    where: list[str] = []
    params: list[Any] = []
    d = str(date or "").strip()
    if d:
        where.append("substr(created_at, 1, 10) = ?")
        params.append(d)
    u = str(username or "").strip()
    if u:
        where.append("username LIKE ? COLLATE NOCASE")
        params.append(f"%{u}%")
    st = str(status or "").strip()
    if st:
        where.append("status = ?")
        params.append(st)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with db_conn() as conn:
        total = int(
            conn.execute(
                f"SELECT COUNT(*) FROM chat_lixi_claims{clause}", params
            ).fetchone()[0]
        )
        amount_filtered = int(
            conn.execute(
                f"SELECT COALESCE(SUM(CASE WHEN status = 'success' THEN money ELSE 0 END), 0) "
                f"FROM chat_lixi_claims{clause}",
                params,
            ).fetchone()[0]
        )
        amount_all = int(
            conn.execute(
                "SELECT COALESCE(SUM(money), 0) FROM chat_lixi_claims WHERE status = 'success'"
            ).fetchone()[0]
        )
        offset = (page - 1) * limit
        rows = conn.execute(
            f"SELECT * FROM chat_lixi_claims{clause} "
            f"ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "rows": [dict(r) for r in rows],
        "total_amount_filtered": amount_filtered,
        "total_amount_all": amount_all,
    }


def sum_today_vn() -> dict[str, Any]:
    """Tổng tiền / số lần success hôm nay (ngày VN)."""
    init_chat_lixi_table()
    day = today_vn_str()
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT
              COALESCE(SUM(money), 0) AS today_amount,
              COUNT(*) AS today_success_count
            FROM chat_lixi_claims
            WHERE status = 'success' AND substr(created_at, 1, 10) = ?
            """,
            (day,),
        ).fetchone()
    return {
        "vn_day": day,
        "today_amount": int(row[0] or 0),
        "today_success_count": int(row[1] or 0),
    }
