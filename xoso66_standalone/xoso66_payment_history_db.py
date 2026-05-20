# -*- coding: utf-8 -*-
"""SQLite — lịch sử nạp/rút (paymentorderlist)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from xoso66_accounts_db import db_conn

_PAYMENT_ORDERS_DDL = """
CREATE TABLE IF NOT EXISTS payment_orders (
    serial_no TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    order_type INTEGER NOT NULL,
    amount REAL NOT NULL DEFAULT 0,
    true_amount REAL NOT NULL DEFAULT 0,
    status INTEGER NOT NULL DEFAULT 0,
    status_formatted TEXT NOT NULL DEFAULT '',
    type_formatted TEXT NOT NULL DEFAULT '',
    category_formatted TEXT NOT NULL DEFAULT '',
    channel_formatted TEXT NOT NULL DEFAULT '',
    create_time TEXT NOT NULL DEFAULT '',
    create_time_ms INTEGER,
    remark TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    currency INTEGER NOT NULL DEFAULT 1,
    currency_formatted TEXT NOT NULL DEFAULT '',
    bank_name_formatted TEXT NOT NULL DEFAULT '',
    account_formatted TEXT NOT NULL DEFAULT '',
    truename_formatted TEXT NOT NULL DEFAULT '',
    usdt_amount REAL NOT NULL DEFAULT 0,
    rebate TEXT NOT NULL DEFAULT '',
    total_promo_amount TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT '{}',
    synced_at TEXT NOT NULL
)
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_payment_create_time_ms(create_time: str) -> int | None:
    s = str(create_time or "").strip()
    if not s:
        return None
    for fmt in ("%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return None


def _float_val(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _payment_orders_columns(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("PRAGMA table_info(payment_orders)").fetchall()
    return {str(r[1]) for r in rows}


def migrate_payment_orders_username(conn: sqlite3.Connection | None = None) -> None:
    """Thêm cột username + backfill từ accounts (idempotent)."""
    if conn is not None:
        _migrate_payment_orders_username_conn(conn)
        return
    with db_conn() as c:
        _migrate_payment_orders_username_conn(c)


def _migrate_payment_orders_username_conn(conn: sqlite3.Connection) -> None:
    conn.execute(_PAYMENT_ORDERS_DDL)
    cols = _payment_orders_columns(conn)
    if "username" not in cols:
        conn.execute(
            "ALTER TABLE payment_orders ADD COLUMN username TEXT NOT NULL DEFAULT ''"
        )
    cols = _payment_orders_columns(conn)
    if "device_balance_credited" not in cols:
        conn.execute(
            "ALTER TABLE payment_orders ADD COLUMN device_balance_credited "
            "INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        """
        UPDATE payment_orders
        SET username = COALESCE(
            NULLIF(
                (SELECT a.username FROM accounts a
                 WHERE a.id = payment_orders.account_id),
                ''
            ),
            account_id
        )
        WHERE COALESCE(username, '') = ''
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_payment_orders_username "
        "ON payment_orders(username)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_payment_orders_user_type "
        "ON payment_orders(username, order_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_payment_orders_account "
        "ON payment_orders(account_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_payment_orders_acct_type "
        "ON payment_orders(account_id, order_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_payment_orders_time "
        "ON payment_orders(account_id, create_time_ms DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS device_withdraw_credits (
            serial_no TEXT PRIMARY KEY,
            account_id TEXT NOT NULL DEFAULT '',
            amount_vnd INTEGER NOT NULL DEFAULT 0,
            device TEXT NOT NULL DEFAULT '',
            credited_at TEXT NOT NULL
        )
        """
    )


def init_payment_history_tables(conn: sqlite3.Connection | None = None) -> None:
    if conn is not None:
        _migrate_payment_orders_username_conn(conn)
        return
    with db_conn() as c:
        init_payment_history_tables(c)


def _username_for_payment_row(account_id: str, item: dict[str, Any]) -> str:
    u = str(item.get("username") or "").strip()
    if u:
        return u
    from xoso66_accounts_db import get_account, username_for_log

    return username_for_log(account_id, get_account(account_id))


def _item_to_row(account_id: str, item: dict[str, Any], order_type: int) -> dict[str, Any]:
    serial = str(item.get("serial_no") or "").strip()
    if not serial:
        raise ValueError("thiếu serial_no")
    create_time = str(item.get("create_time") or "")
    return {
        "serial_no": serial,
        "account_id": str(account_id),
        "username": _username_for_payment_row(account_id, item),
        "order_type": int(item.get("type") or order_type),
        "amount": _float_val(item.get("amount")),
        "true_amount": _float_val(item.get("true_amount") or item.get("amount")),
        "status": int(item.get("status") or 0),
        "status_formatted": str(item.get("status_formatted") or ""),
        "type_formatted": str(item.get("type_formatted") or ""),
        "category_formatted": str(item.get("category_formatted") or ""),
        "channel_formatted": str(item.get("channel_formatted") or ""),
        "create_time": create_time,
        "create_time_ms": parse_payment_create_time_ms(create_time),
        "remark": str(item.get("remark") or ""),
        "note": str(item.get("note") or ""),
        "currency": int(item.get("currency") or 1),
        "currency_formatted": str(item.get("currency_formatted") or ""),
        "bank_name_formatted": str(item.get("bank_name_formatted") or ""),
        "account_formatted": str(item.get("account_formatted") or ""),
        "truename_formatted": str(item.get("truename_formatted") or ""),
        "usdt_amount": _float_val(item.get("usdt_amount")),
        "rebate": str(item.get("rebate") if item.get("rebate") is not None else ""),
        "total_promo_amount": str(item.get("total_promo_amount") or ""),
        "raw_json": json.dumps(item, ensure_ascii=False),
        "synced_at": _now_iso(),
    }


SITE_STATUS_SUCCESS = 1
ORDER_TYPE_DEPOSIT = 1
ORDER_TYPE_WITHDRAW = 2


def payment_order_exists(serial_no: str) -> bool:
    serial = str(serial_no or "").strip()
    if not serial:
        return False
    with db_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM payment_orders WHERE serial_no = ? LIMIT 1",
            (serial,),
        ).fetchone()
    return row is not None


def is_device_balance_credited(serial_no: str) -> bool:
    sn = str(serial_no or "").strip()
    if not sn:
        return False
    with db_conn() as conn:
        migrate_payment_orders_username(conn)
        row = conn.execute(
            "SELECT 1 FROM device_withdraw_credits WHERE serial_no = ? LIMIT 1",
            (sn,),
        ).fetchone()
        if row:
            return True
    row = get_payment_order(sn)
    if not row:
        return False
    return bool(int(row.get("device_balance_credited") or 0))


def mark_device_balance_credited(
    serial_no: str,
    *,
    account_id: str = "",
    amount_vnd: int = 0,
    device: str = "",
) -> None:
    sn = str(serial_no or "").strip()
    if not sn:
        return
    with db_conn() as conn:
        migrate_payment_orders_username(conn)
        conn.execute(
            """
            INSERT INTO device_withdraw_credits (
                serial_no, account_id, amount_vnd, device, credited_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(serial_no) DO NOTHING
            """,
            (sn, str(account_id), int(amount_vnd), str(device), _now_iso()),
        )
        conn.execute(
            "UPDATE payment_orders SET device_balance_credited = 1 WHERE serial_no = ?",
            (sn,),
        )


def get_payment_order(serial_no: str) -> dict[str, Any] | None:
    serial = str(serial_no or "").strip()
    if not serial:
        return None
    with db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM payment_orders WHERE serial_no = ?",
            (serial,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d.pop("raw_json", None)
    return d


def is_deposit_success_in_db(serial_no: str) -> bool:
    row = get_payment_order(serial_no)
    if not row:
        return False
    return int(row.get("order_type") or 0) == ORDER_TYPE_DEPOSIT and int(
        row.get("status") or 0
    ) == SITE_STATUS_SUCCESS


def save_payment_order_on_success(
    account_id: str, item: dict[str, Any], *, order_type: int = ORDER_TYPE_DEPOSIT
) -> bool:
    """Chỉ ghi payment_orders khi Hoàn tất (status=1)."""
    if int(item.get("status") or 0) != SITE_STATUS_SUCCESS:
        return False
    if int(item.get("type") or order_type) != order_type:
        return False
    upsert_payment_order(account_id, item, order_type)
    return True


def sync_deposit_successes_from_list(
    account_id: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    So danh sách API với DB: đơn nạp status=1 chưa có thì insert.
    Dùng để bắt lệnh vừa chuyển Hoàn tất (kể cả khi poll serial_no).
    """
    new_serials: list[str] = []
    updated: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if int(item.get("type") or 0) != ORDER_TYPE_DEPOSIT:
            continue
        if int(item.get("status") or 0) != SITE_STATUS_SUCCESS:
            continue
        serial = str(item.get("serial_no") or "").strip()
        if not serial:
            continue
        existed = payment_order_exists(serial)
        save_payment_order_on_success(account_id, item, order_type=ORDER_TYPE_DEPOSIT)
        if existed:
            updated.append(serial)
        else:
            new_serials.append(serial)
    return {
        "new_serials": new_serials,
        "updated_serials": updated,
        "count_new": len(new_serials),
    }


def sync_withdraw_successes_from_list(
    account_id: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Đơn rút status=1 chưa có DB → insert (giống sync_deposit_successes_from_list)."""
    new_serials: list[str] = []
    updated: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if int(item.get("type") or 0) != ORDER_TYPE_WITHDRAW:
            continue
        if int(item.get("status") or 0) != SITE_STATUS_SUCCESS:
            continue
        serial = str(item.get("serial_no") or "").strip()
        if not serial:
            continue
        existed = payment_order_exists(serial)
        save_payment_order_on_success(account_id, item, order_type=ORDER_TYPE_WITHDRAW)
        if existed:
            updated.append(serial)
        else:
            new_serials.append(serial)
    return {
        "new_serials": new_serials,
        "updated_serials": updated,
        "count_new": len(new_serials),
    }


def _credit_device_if_withdraw_success(
    account_id: str, item: dict[str, Any], order_type: int
) -> None:
    """Rút Hoàn tất vừa ghi DB → cộng tiền lên device_balances (một lần / serial)."""
    if int(order_type) != ORDER_TYPE_WITHDRAW:
        return
    if int(item.get("status") or 0) != SITE_STATUS_SUCCESS:
        return
    serial = str(item.get("serial_no") or "").strip()
    if not serial or is_device_balance_credited(serial):
        return
    try:
        from xoso66_device_balance import credit_device_on_withdraw_saved

        credit_device_on_withdraw_saved(account_id, item)
    except Exception as e:
        print(f"[DEVICE-BAL] lỗi khi ghi DB rút {serial}: {e}", flush=True)


def upsert_payment_order(account_id: str, item: dict[str, Any], order_type: int) -> str:
    """Insert hoặc cập nhật theo serial_no. Rút Hoàn tất → cộng device. Trả serial_no."""
    row = _item_to_row(account_id, item, order_type)
    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO payment_orders (
                serial_no, account_id, username, order_type, amount, true_amount,
                status, status_formatted, type_formatted, category_formatted,
                channel_formatted, create_time, create_time_ms, remark, note,
                currency, currency_formatted, bank_name_formatted,
                account_formatted, truename_formatted, usdt_amount, rebate,
                total_promo_amount, raw_json, synced_at
            ) VALUES (
                :serial_no, :account_id, :username, :order_type, :amount, :true_amount,
                :status, :status_formatted, :type_formatted, :category_formatted,
                :channel_formatted, :create_time, :create_time_ms, :remark, :note,
                :currency, :currency_formatted, :bank_name_formatted,
                :account_formatted, :truename_formatted, :usdt_amount, :rebate,
                :total_promo_amount, :raw_json, :synced_at
            )
            ON CONFLICT(serial_no) DO UPDATE SET
                account_id=excluded.account_id,
                username=excluded.username,
                order_type=excluded.order_type,
                amount=excluded.amount,
                true_amount=excluded.true_amount,
                status=excluded.status,
                status_formatted=excluded.status_formatted,
                type_formatted=excluded.type_formatted,
                category_formatted=excluded.category_formatted,
                channel_formatted=excluded.channel_formatted,
                create_time=excluded.create_time,
                create_time_ms=excluded.create_time_ms,
                remark=excluded.remark,
                note=excluded.note,
                currency=excluded.currency,
                currency_formatted=excluded.currency_formatted,
                bank_name_formatted=excluded.bank_name_formatted,
                account_formatted=excluded.account_formatted,
                truename_formatted=excluded.truename_formatted,
                usdt_amount=excluded.usdt_amount,
                rebate=excluded.rebate,
                total_promo_amount=excluded.total_promo_amount,
                raw_json=excluded.raw_json,
                synced_at=excluded.synced_at
            """,
            row,
        )
    _credit_device_if_withdraw_success(account_id, item, order_type)
    return row["serial_no"]


def upsert_payment_orders(
    account_id: str, items: list[dict[str, Any]], order_type: int
) -> int:
    n = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            upsert_payment_order(account_id, item, order_type)
            n += 1
        except ValueError:
            continue
    return n


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d.pop("raw_json", None)
    return d


def list_payment_orders_all_db(
    *,
    account_id: str | None = None,
    username: str | None = None,
    order_type: int | None = None,
    status: int | None = None,
    page: int = 1,
    limit: int = 50,
) -> dict[str, Any]:
    """Danh sách lịch sử nạp/rút — lọc toàn CMS."""
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
    if order_type is not None:
        where.append("order_type = ?")
        params.append(int(order_type))
    if status is not None:
        where.append("status = ?")
        params.append(int(status))
    wsql = " AND ".join(where)
    with db_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM payment_orders WHERE {wsql}",
            params,
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT * FROM payment_orders
            WHERE {wsql}
            ORDER BY COALESCE(create_time_ms, 0) DESC, serial_no DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    return {
        "total": int(total),
        "page": page,
        "limit": limit,
        "list": [_row_to_dict(r) for r in rows],
    }


def list_payment_orders_db(
    account_id: str,
    *,
    order_type: int | None = None,
    status: int | None = None,
    page: int = 1,
    limit: int = 50,
) -> dict[str, Any]:
    """Lọc theo account_id nội bộ; mỗi dòng trả về có username."""
    page = max(1, int(page))
    limit = max(1, min(200, int(limit)))
    offset = (page - 1) * limit
    where = ["account_id = ?"]
    params: list[Any] = [str(account_id)]
    if order_type is not None:
        where.append("order_type = ?")
        params.append(int(order_type))
    if status is not None:
        where.append("status = ?")
        params.append(int(status))
    wsql = " AND ".join(where)
    with db_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM payment_orders WHERE {wsql}",
            params,
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT * FROM payment_orders
            WHERE {wsql}
            ORDER BY COALESCE(create_time_ms, 0) DESC, serial_no DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    return {
        "total": int(total),
        "page": page,
        "limit": limit,
        "list": [_row_to_dict(r) for r in rows],
    }


def payment_totals_by_account() -> dict[str, dict[str, float]]:
    """Tổng nạp/rút Hoàn tất (status=1) theo account_id — một query cho CMS list."""
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT account_id,
                COALESCE(SUM(CASE WHEN order_type = 1 THEN true_amount ELSE 0 END), 0) AS deposit,
                COALESCE(SUM(CASE WHEN order_type = 2 THEN true_amount ELSE 0 END), 0) AS withdraw
            FROM payment_orders
            WHERE status = 1
            GROUP BY account_id
            """
        ).fetchall()
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        aid = str(r["account_id"] or "").strip()
        if not aid:
            continue
        out[aid] = {
            "deposit": float(r["deposit"] or 0),
            "withdraw": float(r["withdraw"] or 0),
        }
    return out


def sum_completed_amounts(account_id: str) -> dict[str, float]:
    """Tổng amount đơn status=1 (Hoàn tất) theo loại."""
    with db_conn() as conn:
        dep = conn.execute(
            """
            SELECT COALESCE(SUM(true_amount), 0) FROM payment_orders
            WHERE account_id = ? AND order_type = 1 AND status = 1
            """,
            (str(account_id),),
        ).fetchone()[0]
        wd = conn.execute(
            """
            SELECT COALESCE(SUM(true_amount), 0) FROM payment_orders
            WHERE account_id = ? AND order_type = 2 AND status = 1
            """,
            (str(account_id),),
        ).fetchone()[0]
    return {"deposit": float(dep or 0), "withdraw": float(wd or 0)}


def sum_all_account_balances() -> float:
    """Tổng số dư CMS — mọi dòng trong bảng accounts."""
    with db_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(balance), 0) FROM accounts"
        ).fetchone()
    return float(row[0] or 0)


def sum_completed_amounts_all() -> dict[str, float]:
    """Tổng nạp/rút Hoàn tất + tổng số dư mọi account."""
    with db_conn() as conn:
        dep = conn.execute(
            """
            SELECT COALESCE(SUM(true_amount), 0) FROM payment_orders
            WHERE order_type = 1 AND status = 1
            """
        ).fetchone()[0]
        wd = conn.execute(
            """
            SELECT COALESCE(SUM(true_amount), 0) FROM payment_orders
            WHERE order_type = 2 AND status = 1
            """
        ).fetchone()[0]
    return {
        "deposit": float(dep or 0),
        "withdraw": float(wd or 0),
        "balance": sum_all_account_balances(),
    }


def payment_stats(
    account_id: str | None = None,
    *,
    username: str | None = None,
) -> dict[str, Any]:
    """
    Thống kê theo ô lọc: 1 user hoặc toàn hệ thống.
    Số dư: balance account đang lọc, hoặc SUM(balance) khi không lọc.
    Lợi nhuận = tổng rút + số dư − tổng nạp.
    """
    from xoso66_accounts_db import get_account, get_account_by_username

    aid = str(account_id or "").strip()
    if not aid and username:
        acc = get_account_by_username(str(username).strip())
        if acc:
            aid = str(acc.get("id") or "")
    scope = "account"
    if not aid:
        sums = sum_completed_amounts_all()
        dep = sums["deposit"]
        wd = sums["withdraw"]
        bal = sums["balance"]
        scope = "all"
        uname = ""
    else:
        acc = get_account(aid)
        if not acc:
            return {"ok": False, "error": "không tìm thấy account"}
        sums = sum_completed_amounts(aid)
        dep = sums["deposit"]
        wd = sums["withdraw"]
        bal = float(acc.get("balance") or 0)
        uname = str(acc.get("username") or aid)
    profit = wd + bal - dep
    return {
        "ok": True,
        "scope": scope,
        "account_id": aid or None,
        "username": uname,
        "total_deposit": dep,
        "total_withdraw": wd,
        "balance": bal,
        "profit": profit,
    }


def list_payment_orders_by_username(
    username: str,
    *,
    order_type: int | None = None,
    status: int | None = None,
    page: int = 1,
    limit: int = 50,
) -> dict[str, Any]:
    """Lọc theo username (nick game) — tiện xem DB / CMS."""
    from xoso66_accounts_db import get_account_by_username

    acc = get_account_by_username(username)
    if not acc:
        return {"total": 0, "page": max(1, int(page)), "limit": limit, "list": []}
    return list_payment_orders_db(
        str(acc["id"]),
        order_type=order_type,
        status=status,
        page=page,
        limit=limit,
    )
