# -*- coding: utf-8 -*-
"""Lệnh nạp auto — SQLite (local order id; serial_no ghi sau khi Hoàn tất)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from xoso66_accounts_db import db_conn

_DEPOSIT_ORDERS_DDL = """
CREATE TABLE IF NOT EXISTS deposit_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    amount INTEGER NOT NULL DEFAULT 0,
    serial_no TEXT NOT NULL DEFAULT '',
    trade_no TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'Chờ Nạp',
    site_status INTEGER,
    site_status_formatted TEXT NOT NULL DEFAULT '',
    qr_image_path TEXT NOT NULL DEFAULT '',
    transfer_json TEXT NOT NULL DEFAULT '{}',
    third_party_tx_id TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    order_placed_at_ms INTEGER NOT NULL DEFAULT 0,
    da_nap_at_ms INTEGER NOT NULL DEFAULT 0,
    confirm_duration_sec INTEGER,
    device_nap TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def _deposit_orders_columns(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("PRAGMA table_info(deposit_orders)").fetchall()
    return {str(r[1]) for r in rows}


def migrate_deposit_orders_duration(conn: sqlite3.Connection | None = None) -> None:
    """Thêm da_nap_at_ms + confirm_duration_sec (idempotent)."""
    if conn is not None:
        _migrate_deposit_orders_duration_conn(conn)
        return
    with db_conn() as c:
        _migrate_deposit_orders_duration_conn(c)


def _migrate_deposit_orders_duration_conn(conn: sqlite3.Connection) -> None:
    conn.execute(_DEPOSIT_ORDERS_DDL)
    cols = _deposit_orders_columns(conn)
    if "da_nap_at_ms" not in cols:
        conn.execute(
            "ALTER TABLE deposit_orders ADD COLUMN da_nap_at_ms "
            "INTEGER NOT NULL DEFAULT 0"
        )
    if "confirm_duration_sec" not in cols:
        conn.execute(
            "ALTER TABLE deposit_orders ADD COLUMN confirm_duration_sec INTEGER"
        )
    if "device_nap" not in cols:
        conn.execute(
            "ALTER TABLE deposit_orders ADD COLUMN device_nap TEXT NOT NULL DEFAULT ''"
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_deposit_orders_table(conn: sqlite3.Connection | None = None) -> None:
    if conn is not None:
        conn.execute(_DEPOSIT_ORDERS_DDL)
        migrate_deposit_orders_duration(conn)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_deposit_orders_serial "
            "ON deposit_orders(serial_no) WHERE serial_no != ''"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_deposit_orders_account "
            "ON deposit_orders(account_id, created_at DESC)"
        )
        return
    with db_conn() as c:
        init_deposit_orders_table(c)


def create_deposit_order_row(
    *,
    account_id: str,
    username: str,
    amount: int,
    serial_no: str = "",
    trade_no: str = "",
    status: str = "Chờ Nạp",
    order_placed_at_ms: int = 0,
    qr_image_path: str = "",
    transfer_info: dict | None = None,
) -> int:
    now = _now_iso()
    with db_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO deposit_orders (
                account_id, username, amount, serial_no, trade_no, status,
                site_status, site_status_formatted, qr_image_path, transfer_json,
                third_party_tx_id, error_message, order_placed_at_ms,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, '', ?, ?, '', '', ?, ?, ?)
            """,
            (
                str(account_id),
                str(username or ""),
                int(amount),
                str(serial_no or ""),
                str(trade_no or ""),
                str(status),
                str(qr_image_path or ""),
                json.dumps(transfer_info or {}, ensure_ascii=False),
                int(order_placed_at_ms or 0),
                now,
                now,
            ),
        )
        return int(cur.lastrowid)


def update_deposit_order(order_id: int, **fields: Any) -> None:
    allowed = {
        "serial_no",
        "trade_no",
        "status",
        "site_status",
        "site_status_formatted",
        "qr_image_path",
        "transfer_json",
        "third_party_tx_id",
        "error_message",
        "da_nap_at_ms",
        "confirm_duration_sec",
        "device_nap",
    }
    parts: list[str] = []
    vals: list[Any] = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "transfer_json" and isinstance(v, dict):
            v = json.dumps(v, ensure_ascii=False)
        parts.append(f"{k} = ?")
        vals.append(v)
    if not parts:
        return
    parts.append("updated_at = ?")
    vals.append(_now_iso())
    vals.append(int(order_id))
    with db_conn() as conn:
        conn.execute(
            f"UPDATE deposit_orders SET {', '.join(parts)} WHERE id = ?",
            vals,
        )


def mark_deposit_da_nap(order_id: int, *, at_ms: int | None = None, device_nap: str = "") -> None:
    """Ghi mốc bắt đầu đo thời gian nạp — callback bên thứ 3 «Đã Nạp»."""
    from xoso66_confirm_duration import now_ms

    row = get_deposit_order(int(order_id)) or {}
    extra: dict[str, Any] = {}
    dev = str(device_nap or "").strip()
    if dev:
        extra["device_nap"] = dev
    if int(row.get("da_nap_at_ms") or 0) > 0:
        update_deposit_order(int(order_id), status="Đã Nạp", **extra)
        return
    update_deposit_order(
        int(order_id),
        status="Đã Nạp",
        da_nap_at_ms=int(at_ms if at_ms is not None else now_ms()),
        **extra,
    )


def finalize_deposit_success(
    order_id: int,
    *,
    serial_no: str = "",
    site_status: int = 1,
    site_status_formatted: str = "Hoàn tất",
    game_item: dict[str, Any] | None = None,
) -> int:
    """Ghi Thành Công + confirm_duration_sec (Đã Nạp → game Hoàn tất). Trả duration_sec."""
    from xoso66_confirm_duration import (
        compute_confirm_duration_sec,
        game_item_end_ms,
        now_ms,
    )

    row = get_deposit_order(int(order_id)) or {}
    start_ms = int(row.get("da_nap_at_ms") or 0)
    end_ms = game_item_end_ms(game_item)
    if end_ms <= 0:
        end_ms = now_ms()
    duration = compute_confirm_duration_sec(start_ms, end_ms)
    fields: dict[str, Any] = {
        "status": "Thành Công",
        "serial_no": str(serial_no or ""),
        "site_status": int(site_status),
        "site_status_formatted": str(site_status_formatted or "Hoàn tất"),
    }
    if duration > 0:
        fields["confirm_duration_sec"] = duration
    update_deposit_order(int(order_id), **fields)
    return duration


def get_deposit_order(order_id: int) -> dict[str, Any] | None:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM deposit_orders WHERE id = ?",
            (int(order_id),),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["transfer_info"] = json.loads(d.pop("transfer_json") or "{}")
    except json.JSONDecodeError:
        d["transfer_info"] = {}
    return d


def list_deposit_orders_db(
    *,
    account_id: str | None = None,
    username: str | None = None,
    status: str | None = None,
    page: int = 1,
    limit: int = 50,
) -> dict[str, Any]:
    page = max(1, int(page))
    limit = max(1, min(200, int(limit)))
    offset = (page - 1) * limit
    where: list[str] = ["1=1"]
    params: list[Any] = []
    if account_id:
        where.append("account_id = ?")
        params.append(str(account_id))
    if username:
        u = f"%{str(username).strip()}%"
        where.append("(username LIKE ? OR account_id LIKE ?)")
        params.extend([u, u])
    if status:
        where.append("status = ?")
        params.append(str(status).strip())
    wsql = " AND ".join(where)
    with db_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM deposit_orders WHERE {wsql}",
            params,
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT * FROM deposit_orders
            WHERE {wsql}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        try:
            d["transfer_info"] = json.loads(d.pop("transfer_json") or "{}")
        except json.JSONDecodeError:
            d["transfer_info"] = {}
        out.append(d)
    return {"total": int(total), "page": page, "limit": limit, "list": out}


def has_pending_deposit(account_id: str, *, max_age_sec: int = 900) -> bool:
    """Có lệnh chưa kết thúc (tránh tạo trùng)."""
    import time

    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, status, order_placed_at_ms FROM deposit_orders
            WHERE account_id = ?
            ORDER BY id DESC LIMIT 5
            """,
            (str(account_id),),
        ).fetchall()
    terminal = frozenset({"Thành Công", "Thất Bại", "Huỷ"})
    now_ms = int(time.time() * 1000)
    for r in rows:
        st = str(r["status"] or "")
        if st in terminal:
            continue
        placed = int(r["order_placed_at_ms"] or 0)
        if placed and (now_ms - placed) > max_age_sec * 1000:
            continue
        return True
    return False
