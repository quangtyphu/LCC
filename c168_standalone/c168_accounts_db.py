# -*- coding: utf-8 -*-
"""SQLite tài khoản C168 — cùng file CMS/game_data/c168.db (API Node server.js)."""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parent
_DEFAULT_DB = (
    Path(__file__).resolve().parent.parent.parent / "CMS" / "game_data" / "c168.db"
)
DB_PATH = Path(os.environ.get("C168_DB", _DEFAULT_DB))

CMS_COLUMNS = (
    "username",
    "password",
    "phone",
    "account_holder",
    "fund_password",
    "bank_code",
    "bank_name",
    "account_number",
    "proxy",
    "device",
    "balance",
    "daily_bet_total",
    "daily_bet_day",
    "status",
    "chrome_browser_dir",
    "chrome_cdp_port",
    "created_at",
    "updated_at",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db_conn():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def safe_dir_key(username: str) -> str:
    s = re.sub(r"[^\w.\-]+", "_", str(username or "").strip(), flags=re.UNICODE)
    return (s[:120] or "user")


def _browsers_root() -> Path:
    return Path(
        os.environ.get("C168_BROWSERS_DIR", _DEFAULT_DB.parent / "c168_browsers")
    )


def resolve_profile_dir(
    *,
    username: str,
    stored_dir: str = "",
    legacy_id: str = "",
) -> str:
    """Thư mục profile thực tế — không đổi tên khi Chrome đang giữ file (acc1…)."""
    root = _browsers_root()
    user = str(username or "").strip()
    new_dir = root / safe_dir_key(user)
    old_dir = root / str(legacy_id).strip() if legacy_id else None
    stored = Path(stored_dir.strip()) if str(stored_dir or "").strip() else None

    if stored and stored.is_dir():
        return str(stored)
    if new_dir.is_dir():
        return str(new_dir)
    if old_dir and old_dir.is_dir():
        if not new_dir.exists():
            try:
                old_dir.rename(new_dir)
                return str(new_dir)
            except OSError as e:
                print(
                    f"[C168] Giữ profile {old_dir.name} (Chrome đang mở?): {e}",
                    file=sys.stderr,
                )
        return str(old_dir)
    return str(new_dir)


def _migrate_legacy_id_pk(conn: sqlite3.Connection) -> None:
    cols = {
        str(r[1])
        for r in conn.execute("PRAGMA table_info(c168_accounts)").fetchall()
    }
    if "id" not in cols:
        return
    rows = conn.execute("SELECT * FROM c168_accounts").fetchall()
    _browsers_root().mkdir(parents=True, exist_ok=True)
    migrated: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        user = str(d.get("username") or "").strip()
        if not user:
            continue
        old_id = str(d.get("id") or "").strip()
        chrome_dir = resolve_profile_dir(
            username=user,
            stored_dir=str(d.get("chrome_browser_dir") or ""),
            legacy_id=old_id,
        )
        migrated.append({**d, "chrome_browser_dir": chrome_dir})
    conn.execute(
        """
        CREATE TABLE c168_accounts_new (
            username TEXT PRIMARY KEY COLLATE NOCASE,
            password TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '',
            account_holder TEXT NOT NULL DEFAULT '',
            fund_password TEXT NOT NULL DEFAULT '',
            bank_code TEXT NOT NULL DEFAULT '',
            bank_name TEXT NOT NULL DEFAULT '',
            account_number TEXT NOT NULL DEFAULT '',
            proxy TEXT NOT NULL DEFAULT '',
            device TEXT NOT NULL DEFAULT '',
            balance REAL NOT NULL DEFAULT 0,
            daily_bet_total REAL NOT NULL DEFAULT 0,
            daily_bet_day TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'new',
            chrome_browser_dir TEXT NOT NULL DEFAULT '',
            chrome_cdp_port INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    for d in migrated:
        user = str(d["username"]).strip()
        conn.execute(
            """
            INSERT INTO c168_accounts_new (
                username, password, phone, account_holder, fund_password,
                bank_code, bank_name, account_number, proxy, device,
                balance, daily_bet_total, daily_bet_day, status,
                chrome_browser_dir, chrome_cdp_port, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                user,
                d.get("password") or "",
                d.get("phone") or "",
                d.get("account_holder") or "",
                d.get("fund_password") or "",
                d.get("bank_code") or "",
                d.get("bank_name") or "",
                d.get("account_number") or "",
                d.get("proxy") or "",
                d.get("device") or "",
                float(d.get("balance") or 0),
                float(d.get("daily_bet_total") or 0),
                d.get("daily_bet_day") or "",
                d.get("status") or "new",
                d.get("chrome_browser_dir") or "",
                d.get("chrome_cdp_port"),
                d.get("created_at") or _now_iso(),
                d.get("updated_at") or _now_iso(),
            ),
        )
    conn.execute("DROP TABLE c168_accounts")
    conn.execute("ALTER TABLE c168_accounts_new RENAME TO c168_accounts")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_c168_username ON c168_accounts(username)"
    )


def init_db() -> None:
    with db_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS c168_accounts (
                username TEXT PRIMARY KEY COLLATE NOCASE,
                password TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                account_holder TEXT NOT NULL DEFAULT '',
                fund_password TEXT NOT NULL DEFAULT '',
                bank_code TEXT NOT NULL DEFAULT '',
                bank_name TEXT NOT NULL DEFAULT '',
                account_number TEXT NOT NULL DEFAULT '',
                proxy TEXT NOT NULL DEFAULT '',
                device TEXT NOT NULL DEFAULT '',
                balance REAL NOT NULL DEFAULT 0,
                daily_bet_total REAL NOT NULL DEFAULT 0,
                daily_bet_day TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'new',
                chrome_browser_dir TEXT NOT NULL DEFAULT '',
                chrome_cdp_port INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        _migrate_legacy_id_pk(conn)
        cols = {
            str(r[1])
            for r in conn.execute("PRAGMA table_info(c168_accounts)").fetchall()
        }
        if "chrome_browser_dir" not in cols:
            conn.execute(
                "ALTER TABLE c168_accounts ADD COLUMN chrome_browser_dir "
                "TEXT NOT NULL DEFAULT ''"
            )
        if "chrome_cdp_port" not in cols:
            conn.execute(
                "ALTER TABLE c168_accounts ADD COLUMN chrome_cdp_port INTEGER"
            )


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def list_accounts() -> list[dict[str, Any]]:
    init_db()
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM c168_accounts ORDER BY username COLLATE NOCASE"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_account(username: str) -> dict[str, Any] | None:
    init_db()
    with db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM c168_accounts WHERE username = ? COLLATE NOCASE",
            (str(username).strip(),),
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_account_by_username(username: str) -> dict[str, Any] | None:
    return get_account(username)


def update_account(username: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    allowed = {c for c in CMS_COLUMNS if c not in ("username", "created_at")}
    sets = []
    vals: list[Any] = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append(f"{k} = ?")
        vals.append(v)
    if not sets:
        return get_account(username)
    sets.append("updated_at = ?")
    vals.append(_now_iso())
    vals.append(str(username).strip())
    with db_conn() as conn:
        conn.execute(
            f"UPDATE c168_accounts SET {', '.join(sets)} WHERE username = ? COLLATE NOCASE",
            vals,
        )
    return get_account(username)
