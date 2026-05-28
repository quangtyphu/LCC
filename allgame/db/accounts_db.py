# -*- coding: utf-8 -*-
"""SQLite tài khoản thống nhất — mọi cổng game trong một DB."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from allgame.db.constants import (
    ACCOUNT_COLUMNS,
    STATUS_DANG_CHOI,
    STATUS_DU_NGAY,
    STATUS_HET_TIEN,
    STATUS_LOI,
    STATUS_NEW,
    STATUS_TOKEN_LOI,
    TRANSPORT_CHROME,
    VENDOR_IDLE,
)

_DIR = Path(__file__).resolve().parent.parent
# CMS cùng cấp LC79: Documents/CMS (không phải LC79/CMS)
_REPO_ROOT = _DIR.parent
_CMS_DATA = _REPO_ROOT.parent / "CMS" / "game_data"
_DEFAULT_DB = _CMS_DATA / "allgame.db"
DB_PATH = Path(os.environ.get("ALLGAME_DB", _DEFAULT_DB))

_ACCOUNTS_DDL = """
CREATE TABLE IF NOT EXISTS accounts (
    portal_id TEXT NOT NULL,
    username TEXT NOT NULL COLLATE NOCASE,
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
    total_deposit REAL NOT NULL DEFAULT 0,
    total_withdraw REAL NOT NULL DEFAULT 0,
    daily_bet_total REAL NOT NULL DEFAULT 0,
    daily_bet_day TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    transport TEXT NOT NULL DEFAULT 'chrome',
    chrome_browser_dir TEXT NOT NULL DEFAULT '',
    chrome_cdp_port INTEGER,
    vendor_state TEXT NOT NULL DEFAULT 'idle',
    session_json TEXT NOT NULL DEFAULT '{}',
    portal_extra_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (portal_id, username)
)
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_vn() -> str:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


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


def account_session_key(portal_id: str, username: str) -> str:
    p = str(portal_id or "").strip().lower()
    u = str(username or "").strip()
    return f"{p}:{u}" if p and u else ""


def parse_session_key(session_key: str) -> tuple[str, str]:
    s = str(session_key or "").strip()
    if ":" not in s:
        return "", s
    portal_id, username = s.split(":", 1)
    return portal_id.strip().lower(), username.strip()


def safe_dir_key(username: str) -> str:
    s = re.sub(r"[^\w.\-]+", "_", str(username or "").strip(), flags=re.UNICODE)
    return (s[:120] or "user")


def browsers_root() -> Path:
    return Path(
        os.environ.get(
            "ALLGAME_BROWSERS_DIR",
            DB_PATH.parent / "allgame_browsers",
        )
    )


def resolve_profile_dir(
    *,
    portal_id: str,
    username: str,
    stored_dir: str = "",
) -> str:
    root = browsers_root()
    portal = str(portal_id or "").strip().lower() or "unknown"
    user = str(username or "").strip()
    new_dir = root / portal / safe_dir_key(user)
    stored = Path(stored_dir.strip()) if str(stored_dir or "").strip() else None
    if stored and stored.is_dir():
        return str(stored)
    if new_dir.is_dir():
        return str(new_dir)
    return str(new_dir)


def init_db() -> None:
    with db_conn() as conn:
        conn.execute(_ACCOUNTS_DDL)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_accounts_status "
            "ON accounts(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_accounts_portal_status "
            "ON accounts(portal_id, status)"
        )


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for key in ("session_json", "portal_extra_json"):
        raw = d.get(key) or "{}"
        if isinstance(raw, str):
            try:
                d[key] = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                d[key] = {}
    return d


def get_account(portal_id: str, username: str) -> dict[str, Any] | None:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE portal_id = ? AND username = ? COLLATE NOCASE",
            (str(portal_id).strip().lower(), str(username).strip()),
        ).fetchone()
    return _row_to_dict(row)


def list_accounts(*, portal_id: str | None = None) -> list[dict[str, Any]]:
    with db_conn() as conn:
        if portal_id:
            rows = conn.execute(
                "SELECT * FROM accounts WHERE portal_id = ? ORDER BY username",
                (str(portal_id).strip().lower(),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM accounts ORDER BY portal_id, username"
            ).fetchall()
    return [_row_to_dict(r) or {} for r in rows]


def list_accounts_by_status(
    status: str,
    *,
    portal_id: str | None = None,
) -> list[dict[str, Any]]:
    with db_conn() as conn:
        if portal_id:
            rows = conn.execute(
                "SELECT * FROM accounts WHERE portal_id = ? AND status = ? "
                "ORDER BY username",
                (str(portal_id).strip().lower(), status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM accounts WHERE status = ? ORDER BY portal_id, username",
                (status,),
            ).fetchall()
    return [_row_to_dict(r) or {} for r in rows]


def list_playing_accounts(*, portal_id: str | None = None) -> list[dict[str, Any]]:
    return list_accounts_by_status(STATUS_DANG_CHOI, portal_id=portal_id)


def set_account_status(
    portal_id: str,
    username: str,
    status: str,
    *,
    vendor_state: str | None = None,
) -> bool:
    now = _now_iso()
    with db_conn() as conn:
        if vendor_state is not None:
            cur = conn.execute(
                """
                UPDATE accounts SET status = ?, vendor_state = ?, updated_at = ?
                WHERE portal_id = ? AND username = ? COLLATE NOCASE
                """,
                (
                    status,
                    vendor_state,
                    now,
                    str(portal_id).strip().lower(),
                    str(username).strip(),
                ),
            )
        else:
            cur = conn.execute(
                """
                UPDATE accounts SET status = ?, updated_at = ?
                WHERE portal_id = ? AND username = ? COLLATE NOCASE
                """,
                (
                    status,
                    now,
                    str(portal_id).strip().lower(),
                    str(username).strip(),
                ),
            )
    return cur.rowcount > 0


def upsert_account(fields: dict[str, Any]) -> dict[str, Any]:
    portal_id = str(fields.get("portal_id") or "").strip().lower()
    username = str(fields.get("username") or "").strip()
    if not portal_id or not username:
        raise ValueError("Thiếu portal_id hoặc username")

    existing = get_account(portal_id, username)
    now = _now_iso()
    created = (existing or {}).get("created_at") or now

    session = fields.get("session_json")
    if isinstance(session, dict):
        session_s = json.dumps(session, ensure_ascii=False)
    elif session is None:
        session_s = json.dumps((existing or {}).get("session_json") or {}, ensure_ascii=False)
    else:
        session_s = str(session)

    extra = fields.get("portal_extra_json")
    if isinstance(extra, dict):
        extra_s = json.dumps(extra, ensure_ascii=False)
    elif extra is None:
        extra_s = json.dumps((existing or {}).get("portal_extra_json") or {}, ensure_ascii=False)
    else:
        extra_s = str(extra)

    chrome_dir = fields.get("chrome_browser_dir")
    if chrome_dir is None and not (existing or {}).get("chrome_browser_dir"):
        chrome_dir = resolve_profile_dir(portal_id=portal_id, username=username)
        try:
            Path(chrome_dir).mkdir(parents=True, exist_ok=True)
            meta_path = Path(chrome_dir) / ".allgame_profile.json"
            meta_path.write_text(
                json.dumps(
                    {
                        "portal_id": portal_id,
                        "username": username,
                        "chrome_browser_dir": chrome_dir,
                        "updated_at": now,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass
    elif chrome_dir is None:
        chrome_dir = (existing or {}).get("chrome_browser_dir") or ""

    values = {
        "portal_id": portal_id,
        "username": username,
        "password": fields.get("password", (existing or {}).get("password", "")),
        "phone": fields.get("phone", (existing or {}).get("phone", "")),
        "account_holder": fields.get(
            "account_holder", (existing or {}).get("account_holder", "")
        ),
        "fund_password": fields.get(
            "fund_password", (existing or {}).get("fund_password", "")
        ),
        "bank_code": fields.get("bank_code", (existing or {}).get("bank_code", "")),
        "bank_name": fields.get("bank_name", (existing or {}).get("bank_name", "")),
        "account_number": fields.get(
            "account_number", (existing or {}).get("account_number", "")
        ),
        "proxy": fields.get("proxy", (existing or {}).get("proxy", "")),
        "device": fields.get("device", (existing or {}).get("device", "")),
        "balance": float(fields.get("balance", (existing or {}).get("balance", 0)) or 0),
        "total_deposit": float(
            fields.get("total_deposit", (existing or {}).get("total_deposit", 0)) or 0
        ),
        "total_withdraw": float(
            fields.get("total_withdraw", (existing or {}).get("total_withdraw", 0)) or 0
        ),
        "daily_bet_total": float(
            fields.get("daily_bet_total", (existing or {}).get("daily_bet_total", 0)) or 0
        ),
        "daily_bet_day": str(
            fields.get("daily_bet_day", (existing or {}).get("daily_bet_day", "")) or ""
        ),
        "status": str(fields.get("status", (existing or {}).get("status", STATUS_NEW))),
        "transport": str(
            fields.get("transport", (existing or {}).get("transport", TRANSPORT_CHROME))
        ),
        "chrome_browser_dir": str(chrome_dir or ""),
        "chrome_cdp_port": fields.get(
            "chrome_cdp_port", (existing or {}).get("chrome_cdp_port")
        ),
        "vendor_state": str(
            fields.get("vendor_state", (existing or {}).get("vendor_state", VENDOR_IDLE))
        ),
        "session_json": session_s,
        "portal_extra_json": extra_s,
        "created_at": created,
        "updated_at": now,
    }

    cols = [c for c in ACCOUNT_COLUMNS if c in values]
    placeholders = ", ".join("?" for _ in cols)
    col_names = ", ".join(cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in ("portal_id", "username"))

    with db_conn() as conn:
        conn.execute(
            f"""
            INSERT INTO accounts ({col_names}) VALUES ({placeholders})
            ON CONFLICT(portal_id, username) DO UPDATE SET {updates}
            """,
            tuple(values[c] for c in cols),
        )
    out = get_account(portal_id, username)
    return out or values


def update_session(
    portal_id: str,
    username: str,
    session_patch: dict[str, Any],
) -> dict[str, Any] | None:
    acc = get_account(portal_id, username)
    if not acc:
        return None
    base = acc.get("session_json") if isinstance(acc.get("session_json"), dict) else {}
    merged = {**base, **session_patch}
    return upsert_account(
        {
            "portal_id": portal_id,
            "username": username,
            "session_json": merged,
        }
    )


def daily_bet_today_vnd(row: dict[str, Any]) -> float:
    today = _today_vn()
    if str(row.get("daily_bet_day") or "") != today:
        return 0.0
    return float(row.get("daily_bet_total") or 0)


def record_daily_bet(portal_id: str, username: str, amount_vnd: int) -> None:
    today = _today_vn()
    acc = get_account(portal_id, username) or {}
    day = str(acc.get("daily_bet_day") or "")
    total = float(acc.get("daily_bet_total") or 0)
    if day != today:
        total = 0.0
    upsert_account(
        {
            "portal_id": portal_id,
            "username": username,
            "daily_bet_day": today,
            "daily_bet_total": total + max(0, int(amount_vnd)),
        }
    )


__all__ = [
    "DB_PATH",
    "STATUS_DANG_CHOI",
    "STATUS_DU_NGAY",
    "STATUS_HET_TIEN",
    "STATUS_LOI",
    "STATUS_NEW",
    "STATUS_TOKEN_LOI",
    "account_session_key",
    "browsers_root",
    "daily_bet_today_vnd",
    "db_conn",
    "get_account",
    "init_db",
    "list_accounts",
    "list_accounts_by_status",
    "list_playing_accounts",
    "parse_session_key",
    "record_daily_bet",
    "resolve_profile_dir",
    "safe_dir_key",
    "set_account_status",
    "update_session",
    "upsert_account",
]
