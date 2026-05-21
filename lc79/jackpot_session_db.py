# jackpot_session_db.py
"""
Bảng SQLite jackpot_session_records — lưu phiên nổ hũ và thông số chia tiền.

Công thức: amount_received = (my_total_bet * jackpot_amount) / game_total_bet
(game_total_bet = tổng một bên Tài hoặc Xỉu toàn bàn).

Đường dẫn DB trùng với CMS: server.js mở `./game_data.db` (thư mục chứa server.js),
tức thường là `.../Documents/CMS/game_data.db` — cùng cấp với thư mục LC79.

Ghi đè bằng biến môi trường GAME_DATA_DB nếu cần.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


def default_game_data_db_path() -> str:
    """game_data.db nằm cạnh server.js trong thư mục CMS."""
    from constants import REPO_ROOT

    cms_dir = REPO_ROOT.parent / "CMS"
    return str(cms_dir / "game_data.db")


DB_PATH = os.environ.get("GAME_DATA_DB", default_game_data_db_path())
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

_TABLE = "jackpot_session_records"


def _now_str() -> str:
    return datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            session_id INTEGER PRIMARY KEY,
            username TEXT,
            my_total_bet REAL NOT NULL,
            game_total_bet REAL NOT NULL,
            jackpot_amount REAL NOT NULL,
            amount_received REAL NOT NULL,
            jackpot_side TEXT,
            session_timestamp TEXT,
            api_username TEXT,
            dices TEXT,
            dice_point INTEGER,
            overall_total_amount REAL,
            created_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    cur = conn.execute(f"PRAGMA table_info({_TABLE})")
    existing = {row[1] for row in cur.fetchall()}
    for col, typ in (
        ("api_username", "TEXT"),
        ("dices", "TEXT"),
        ("dice_point", "INTEGER"),
        ("overall_total_amount", "REAL"),
        ("created_at", "TEXT"),
    ):
        if col not in existing:
            try:
                conn.execute(f"ALTER TABLE {_TABLE} ADD COLUMN {col} {typ}")
            except sqlite3.Error:
                pass
    conn.commit()


def compute_amount_received(my_bet: float, jackpot: float, game_total: float) -> float:
    if game_total <= 0:
        return 0.0
    return (my_bet * jackpot) / game_total


def upsert_jackpot_record(
    session_id: int,
    username: str,
    my_total_bet: float,
    game_total_bet: float,
    jackpot_amount: float,
    amount_received: float | None = None,
    jackpot_side: str | None = None,
    session_timestamp: str | None = None,
    *,
    api_username: str | None = None,
    dices: list | tuple | None = None,
    dice_point: int | None = None,
    overall_total_amount: float | None = None,
) -> None:
    if amount_received is None:
        amount_received = compute_amount_received(
            my_total_bet, jackpot_amount, game_total_bet
        )
    updated_at = _now_str()
    dices_json: str | None
    if dices is None:
        dices_json = None
    else:
        dices_json = json.dumps([int(x) for x in dices])

    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)
    prev = conn.execute(
        f"SELECT created_at FROM {_TABLE} WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    created_at = (prev[0] if prev and prev[0] else None) or updated_at

    conn.execute(
        f"""
        INSERT INTO {_TABLE} (
            session_id, username, my_total_bet, game_total_bet, jackpot_amount,
            amount_received, jackpot_side, session_timestamp, api_username, dices,
            dice_point, overall_total_amount, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            username = excluded.username,
            my_total_bet = excluded.my_total_bet,
            game_total_bet = excluded.game_total_bet,
            jackpot_amount = excluded.jackpot_amount,
            amount_received = excluded.amount_received,
            jackpot_side = excluded.jackpot_side,
            session_timestamp = excluded.session_timestamp,
            api_username = excluded.api_username,
            dices = excluded.dices,
            dice_point = excluded.dice_point,
            overall_total_amount = excluded.overall_total_amount,
            updated_at = excluded.updated_at
        """,
        (
            session_id,
            username,
            my_total_bet,
            game_total_bet,
            jackpot_amount,
            amount_received,
            jackpot_side,
            session_timestamp,
            api_username,
            dices_json,
            dice_point,
            overall_total_amount,
            created_at,
            updated_at,
        ),
    )
    conn.commit()
    conn.close()


def get_jackpot_record(session_id: int) -> dict[str, Any] | None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    cur = conn.execute(
        f"SELECT * FROM {_TABLE} WHERE session_id = ?",
        (session_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    d: dict[str, Any] = dict(row)
    if d.get("dices"):
        try:
            d["dices"] = json.loads(d["dices"])
        except (json.JSONDecodeError, TypeError):
            pass
    return d


def list_jackpot_records(limit: int = 100) -> list[dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    cur = conn.execute(
        f"SELECT * FROM {_TABLE} ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    for d in rows:
        if d.get("dices"):
            try:
                d["dices"] = json.loads(d["dices"])
            except (json.JSONDecodeError, TypeError):
                pass
    return rows
