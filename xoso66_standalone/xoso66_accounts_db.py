# -*- coding: utf-8 -*-
"""SQLite lưu tài khoản XOSO66 + session runtime (JSON)."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parent
from xoso66_paths import cms_game_data_dir, default_db_path

DATA_DIR = Path(os.environ.get("XOSO66_DATA_DIR") or str(cms_game_data_dir()))
DB_PATH = Path(os.environ.get("XOSO66_DB") or str(default_db_path()))

CMS_COLUMNS = (
    "id",
    "username",
    "password",
    "phone",
    "account_holder",
    "fund_password",
    "bank_code",
    "bank_name",
    "account_number",
    "proxy",
    "default_card_id",
    "device",
    "total_deposit",
    "total_withdraw",
    "balance",
    "status",
    "vip_level",
    "vip_progress",
    "daily_bet_total",
    "daily_bet_day",
    "created_at",
    "updated_at",
)

SENSITIVE_KEYS = frozenset({"password", "fund_password"})

# Tránh lost-update: 56 luồng startup ghi session_json cùng lúc → đọc DB cũ ghi đè token.
_ACCOUNT_WRITE_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
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


_ACCOUNTS_DDL = """
CREATE TABLE accounts (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    account_holder TEXT NOT NULL DEFAULT '',
    fund_password TEXT NOT NULL DEFAULT '',
    bank_code TEXT NOT NULL DEFAULT '',
    bank_name TEXT NOT NULL DEFAULT '',
    account_number TEXT NOT NULL DEFAULT '',
    proxy TEXT NOT NULL DEFAULT '',
    default_card_id INTEGER,
    device TEXT NOT NULL DEFAULT '',
    total_deposit REAL NOT NULL DEFAULT 0,
    total_withdraw REAL NOT NULL DEFAULT 0,
    balance REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'new',
    vip_level TEXT NOT NULL DEFAULT '',
    vip_progress INTEGER NOT NULL DEFAULT 0,
    daily_bet_total REAL NOT NULL DEFAULT 0,
    daily_bet_day TEXT NOT NULL DEFAULT '',
    session_json TEXT NOT NULL DEFAULT '{}',
    provision_log TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def _today_str() -> str:
    from xoso66_time_util import today_vn_str

    return today_vn_str()


def _account_table_columns(conn: sqlite3.Connection) -> set[str]:
    return {str(r[1]) for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}


def _migrate_minigame_daily_bets_to_accounts(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='minigame_daily_bets'"
    ).fetchone()
    if not row:
        return
    today = _today_str()
    try:
        rows = conn.execute(
            "SELECT account_id, total_vnd, bet_day FROM minigame_daily_bets"
        ).fetchall()
        for r in rows:
            aid = str(r["account_id"])
            day = str(r["bet_day"] or "")
            if day != today:
                continue
            total = float(r["total_vnd"] or 0)
            conn.execute(
                """
                UPDATE accounts SET daily_bet_total = ?, daily_bet_day = ?
                WHERE id = ?
                """,
                (total, today, aid),
            )
    except Exception:
        pass
    conn.execute("DROP TABLE IF EXISTS minigame_daily_bets")


def _rebuild_accounts_table(conn: sqlite3.Connection) -> None:
    """Bỏ channel_id / merchant_id / random_remark; thêm daily_bet_*."""
    cols = _account_table_columns(conn)
    if not cols:
        conn.execute(_ACCOUNTS_DDL)
        return

    sel = [
        "id",
        "username",
        "password",
        "phone",
        "account_holder",
        "fund_password",
        "bank_code",
        "bank_name",
        "account_number",
        "proxy",
        "default_card_id",
        "device",
        "total_deposit",
        "total_withdraw",
        "balance",
        "status",
        "vip_level",
    ]
    if "daily_bet_total" in cols:
        sel.append("COALESCE(daily_bet_total, 0)")
    else:
        sel.append("0")
    if "daily_bet_day" in cols:
        sel.append("COALESCE(daily_bet_day, '')")
    else:
        sel.append("''")
    sel.extend(["session_json", "provision_log", "created_at", "updated_at"])

    conn.execute("ALTER TABLE accounts RENAME TO accounts_old")
    conn.execute(_ACCOUNTS_DDL)
    conn.execute(
        f"""
        INSERT INTO accounts (
            id, username, password, phone, account_holder, fund_password,
            bank_code, bank_name, account_number, proxy, default_card_id,
            device, total_deposit, total_withdraw, balance, status, vip_level,
            daily_bet_total, daily_bet_day, session_json, provision_log,
            created_at, updated_at
        )
        SELECT {", ".join(sel)} FROM accounts_old
        """
    )
    conn.execute("DROP TABLE accounts_old")


def migrate_accounts_schema(conn: sqlite3.Connection | None = None) -> list[str]:
    """
    Bỏ channel_id / merchant_id / random_remark; thêm daily_bet_total / daily_bet_day.
    Trả danh sách cột sau migrate.
    """
    if conn is not None:
        _migrate_accounts_schema(conn)
        return sorted(_account_table_columns(conn))

    with db_conn() as c:
        _migrate_accounts_schema(c)
        return sorted(_account_table_columns(c))


def _migrate_accounts_schema(conn: sqlite3.Connection) -> None:
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='accounts'"
    ).fetchone():
        conn.execute(_ACCOUNTS_DDL)
        _migrate_minigame_daily_bets_to_accounts(conn)
        return

    cols = _account_table_columns(conn)
    obsolete = {"channel_id", "merchant_id", "random_remark"}
    need_rebuild = bool(obsolete & cols)
    need_daily = "daily_bet_total" not in cols or "daily_bet_day" not in cols
    need_vip_progress = "vip_progress" not in cols

    if need_rebuild:
        _rebuild_accounts_table(conn)
    elif need_daily:
        if "daily_bet_total" not in cols:
            conn.execute(
                "ALTER TABLE accounts ADD COLUMN daily_bet_total "
                "REAL NOT NULL DEFAULT 0"
            )
        if "daily_bet_day" not in cols:
            conn.execute(
                "ALTER TABLE accounts ADD COLUMN daily_bet_day "
                "TEXT NOT NULL DEFAULT ''"
            )
    if need_vip_progress and "vip_progress" not in _account_table_columns(conn):
        conn.execute(
            "ALTER TABLE accounts ADD COLUMN vip_progress "
            "INTEGER NOT NULL DEFAULT 0"
        )

    cols = _account_table_columns(conn)
    if "daily_bet_total" in cols:
        _migrate_minigame_daily_bets_to_accounts(conn)


def init_db() -> None:
    with db_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                account_holder TEXT NOT NULL DEFAULT '',
                fund_password TEXT NOT NULL DEFAULT '',
                bank_code TEXT NOT NULL DEFAULT '',
                bank_name TEXT NOT NULL DEFAULT '',
                account_number TEXT NOT NULL DEFAULT '',
                proxy TEXT NOT NULL DEFAULT '',
                default_card_id INTEGER,
                device TEXT NOT NULL DEFAULT '',
                total_deposit REAL NOT NULL DEFAULT 0,
                total_withdraw REAL NOT NULL DEFAULT 0,
                balance REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'new',
                vip_level TEXT NOT NULL DEFAULT '',
                vip_progress INTEGER NOT NULL DEFAULT 0,
                daily_bet_total REAL NOT NULL DEFAULT 0,
                daily_bet_day TEXT NOT NULL DEFAULT '',
                session_json TEXT NOT NULL DEFAULT '{}',
                provision_log TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_accounts_username ON accounts(username)"
        )
        _migrate_accounts_schema(conn)
        from xoso66_deposit_orders_db import init_deposit_orders_table
        from xoso66_payment_history_db import init_payment_history_tables

        init_payment_history_tables(conn)
        init_deposit_orders_table(conn)
        from xoso66_mission_db import init_mission_table

        init_mission_table(conn)
        from xoso66_vip_db import init_vip_table

        init_vip_table(conn)
        from xoso66_auto_mission_reward import init_mission_claim_queue

        init_mission_claim_queue(conn)


def next_account_id() -> str:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM accounts WHERE id GLOB 'acc[0-9]*'"
        ).fetchall()
    nums = []
    for r in rows:
        m = re.match(r"^acc(\d+)$", str(r["id"]), re.I)
        if m:
            nums.append(int(m.group(1)))
    n = max(nums, default=0) + 1
    return f"acc{n}"


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["session_json"] = json.loads(d.get("session_json") or "{}")
    d["provision_log"] = json.loads(d.get("provision_log") or "[]")
    return d


def account_to_session_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Gộp cột CMS + session_json → dict dùng cho xoso66_session / deposit."""
    sess = dict(row.get("session_json") or {})
    sess["id"] = row["id"]
    sess["username"] = row.get("username") or sess.get("username")
    sess["password"] = row.get("password") or sess.get("password")
    sess["phone"] = row.get("phone") or sess.get("phone")
    sess["proxy"] = row.get("proxy") or sess.get("proxy")
    if row.get("fund_password"):
        sess["fund_password"] = row["fund_password"]
    if row.get("account_holder"):
        sess["account_holder"] = row["account_holder"]
    if row.get("default_card_id"):
        sess["default_card_id"] = row["default_card_id"]
    if row.get("linked_banks"):
        sess["linked_banks"] = row["linked_banks"]
    return sess


def load_all_as_sessions() -> dict[str, dict]:
    init_db()
    with db_conn() as conn:
        rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    return {str(r["id"]): account_to_session_dict(_row_to_dict(r)) for r in rows}


def get_account(account_id: str) -> dict[str, Any] | None:
    init_db()
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return _row_to_dict(row) if row else None


def username_for_log(account_id: str = "", row: dict[str, Any] | None = None) -> str:
    """Nhãn log/UI — chỉ username (không in account id kiểu acc17777)."""
    if row is None and account_id:
        row = get_account(str(account_id).strip())
    if isinstance(row, dict):
        u = str(row.get("username") or "").strip()
        if u:
            return u
    return "?"


def usernames_for_log(account_ids: list[str]) -> list[str]:
    return [username_for_log(aid) for aid in account_ids if str(aid).strip()]


def get_account_by_username(username: str) -> dict[str, Any] | None:
    """Tìm account theo username (không phân biệt hoa thường)."""
    u = str(username or "").strip()
    if not u:
        return None
    init_db()
    with db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE username = ? COLLATE NOCASE",
            (u,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_accounts() -> list[dict[str, Any]]:
    init_db()
    with db_conn() as conn:
        rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    return [_row_to_dict(r) for r in rows]


STATUS_DANG_CHOI = "Đang Chơi"
STATUS_HET_TIEN = "Hết Tiền"
STATUS_DU_NGAY = "Đủ ngày"
STATUS_LOI = "Lỗi"
STATUS_LOI_PROXY = "Lỗi proxy"

# Chỉ các reason từ WS pool / strategy 3 mới hẹn auto nhận thưởng (không bootstrap lúc mở app).
MISSION_CLAIM_WS_REASONS = frozenset({
    "ngắt WS",
    "đủ cap cược ngày",
    "đạt ngưỡng rút strategy 3",
})


def set_account_status(
    account_id: str,
    status: str,
    *,
    reason: str = "",
) -> bool:
    """Ghi status CMS/DB; trả True nếu đổi."""
    aid = str(account_id).strip()
    new_st = str(status or "").strip()
    if not aid or not new_st:
        return False
    row = get_account(aid)
    if not row:
        return False
    old_st = str(row.get("status") or "").strip()
    if old_st == new_st:
        return False
    update_account(aid, {"status": new_st})
    tag = f" — {reason}" if reason else ""
    print(
        f"[ACCOUNT] {username_for_log(aid, row)}: {old_st or '(trống)'} → {new_st}{tag}",
        flush=True,
    )
    if new_st == STATUS_DU_NGAY and str(reason or "").strip() in MISSION_CLAIM_WS_REASONS:
        try:
            from xoso66_auto_mission_reward import schedule_mission_claim

            schedule_mission_claim(aid, reason=reason)
        except Exception as e:
            print(f"[ACCOUNT] Hẹn auto nhận thưởng: {e}", flush=True)
    return True


def list_accounts_by_status(status: str) -> list[dict[str, Any]]:
    s = str(status or "").strip()
    if not s:
        return []
    init_db()
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM accounts WHERE status = ? ORDER BY id",
            (s,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def pick_random_accounts_by_status(
    count: int,
    *,
    status: str = STATUS_DANG_CHOI,
    require_proxy: bool = True,
    require_minigame: bool = False,
) -> list[dict[str, Any]]:
    """Chọn ngẫu nhiên `count` account (không trùng id)."""
    import random

    pool = list_accounts_by_status(status)
    if require_proxy:
        pool = [a for a in pool if str(a.get("proxy") or "").strip()]
    if require_minigame:
        pool = [
            a
            for a in pool
            if isinstance(a.get("session_json"), dict)
            and (a["session_json"].get("minigame") or {}).get("user_token")
        ]
    if len(pool) < count:
        raise RuntimeError(
            f"Cần {count} account status='{status}'"
            f"{', có proxy' if require_proxy else ''}"
            f" — chỉ có {len(pool)} trong DB."
        )
    return random.sample(pool, count)


def daily_bet_today_vnd(row: dict[str, Any]) -> float:
    today = _today_str()
    if str(row.get("daily_bet_day") or "") != today:
        return 0.0
    return float(row.get("daily_bet_total") or 0)


def list_accounts_by_status_sorted_by_balance(
    status: str,
    *,
    require_proxy: bool = True,
) -> list[dict[str, Any]]:
    """Tất cả acc theo status, sắp balance giảm dần."""
    pool = list_accounts_by_status(status)
    if require_proxy:
        pool = [a for a in pool if str(a.get("proxy") or "").strip()]
    pool.sort(
        key=lambda a: (
            -float(a.get("balance") or 0),
            str(a.get("username") or ""),
            str(a.get("id") or ""),
        )
    )
    return pool


def list_accounts_by_status_sorted_by_daily_bet(
    status: str,
    *,
    require_proxy: bool = True,
) -> list[dict[str, Any]]:
    """Tất cả acc theo status, sắp tổng cược ngày (hôm nay) giảm dần."""
    pool = list_accounts_by_status(status)
    if require_proxy:
        pool = [a for a in pool if str(a.get("proxy") or "").strip()]
    pool.sort(
        key=lambda a: (
            -daily_bet_today_vnd(a),
            -float(a.get("balance") or 0),
            str(a.get("username") or ""),
            str(a.get("id") or ""),
        )
    )
    return pool


def _defaults(payload: dict[str, Any], *, new_id: str | None = None) -> dict[str, Any]:
    now = _now_iso()
    return {
        "id": new_id or next_account_id(),
        "username": str(payload.get("username") or "").strip(),
        "password": str(payload.get("password") or ""),
        "phone": str(payload.get("phone") or ""),
        "account_holder": str(payload.get("account_holder") or payload.get("truename") or ""),
        "fund_password": str(payload.get("fund_password") or ""),
        "bank_code": str(payload.get("bank_code") or ""),
        "bank_name": str(payload.get("bank_name") or ""),
        "account_number": str(payload.get("account_number") or ""),
        "proxy": str(payload.get("proxy") or ""),
        "default_card_id": payload.get("default_card_id"),
        "device": str(payload.get("device") or ""),
        "total_deposit": float(payload.get("total_deposit") or 0),
        "total_withdraw": float(payload.get("total_withdraw") or 0),
        "balance": float(payload.get("balance") or 0),
        "status": str(payload.get("status") or "new"),
        "vip_level": str(payload.get("vip_level") or ""),
        "vip_progress": int(payload.get("vip_progress") or 0),
        "daily_bet_total": float(payload.get("daily_bet_total") or 0),
        "daily_bet_day": str(payload.get("daily_bet_day") or ""),
        "session_json": payload.get("session_json") if isinstance(payload.get("session_json"), dict) else {},
        "provision_log": payload.get("provision_log") if isinstance(payload.get("provision_log"), list) else [],
        "created_at": str(payload.get("created_at") or now),
        "updated_at": str(payload.get("updated_at") or now),
    }


def create_account(payload: dict[str, Any]) -> dict[str, Any]:
    """Tạo row mới — id luôn tự sinh acc{N}, bỏ qua payload['id']."""
    init_db()
    row = _defaults(payload, new_id=next_account_id())
    if not row["username"]:
        raise ValueError("username bắt buộc")
    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO accounts (
                id, username, password, phone, account_holder, fund_password,
                bank_code, bank_name, account_number, proxy, default_card_id,
                device, total_deposit, total_withdraw, balance, status, vip_level,
                daily_bet_total, daily_bet_day, session_json, provision_log,
                created_at, updated_at
            ) VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
            """,
            (
                row["id"],
                row["username"],
                row["password"],
                row["phone"],
                row["account_holder"],
                row["fund_password"],
                row["bank_code"],
                row["bank_name"],
                row["account_number"],
                row["proxy"],
                row["default_card_id"],
                row["device"],
                row["total_deposit"],
                row["total_withdraw"],
                row["balance"],
                row["status"],
                row["vip_level"],
                row["daily_bet_total"],
                row["daily_bet_day"],
                json.dumps(row["session_json"], ensure_ascii=False),
                json.dumps(row["provision_log"], ensure_ascii=False),
                row["created_at"],
                row["updated_at"],
            ),
        )
    return get_account(row["id"])  # type: ignore[return-value]


def adjust_account_balance(account_id: str, delta_vnd: float) -> dict[str, Any]:
    """Cộng/trừ balance CMS (sau kết quả phiên)."""
    cur = get_account(account_id)
    if not cur:
        raise KeyError(f"Không có account '{account_id}'")
    new_bal = float(cur.get("balance") or 0) + float(delta_vnd)
    return update_account(account_id, {"balance": new_bal})


def update_account(account_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    init_db()
    with _ACCOUNT_WRITE_LOCK:
        cur = get_account(account_id)
        if not cur:
            raise KeyError(f"Không có account '{account_id}'")

        allowed = set(CMS_COLUMNS) | {"session_json", "provision_log", "linked_banks"}
        updates: dict[str, Any] = {}
        for k, v in patch.items():
            if k == "id":
                continue
            if k not in allowed:
                continue
            if k == "session_json" and isinstance(v, dict):
                merged = dict(cur.get("session_json") or {})
                for sk, sv in v.items():
                    if sk == "minigame" and isinstance(sv, dict):
                        merged["minigame"] = _merge_minigame_dict(
                            merged.get("minigame") if isinstance(merged.get("minigame"), dict) else {},
                            sv,
                        )
                    else:
                        merged[sk] = sv
                updates["session_json"] = merged
            elif k == "provision_log" and isinstance(v, list):
                updates["provision_log"] = v
            elif k == "linked_banks":
                # Gộp vào session_json đang build — tránh ghi đè minigame/token vừa merge ở nhánh session_json.
                sj = dict(updates.get("session_json") or cur.get("session_json") or {})
                sj["linked_banks"] = v
                updates["session_json"] = sj
            else:
                updates[k] = v

        if not updates:
            return cur

        updates["updated_at"] = _now_iso()
        sets = []
        vals: list[Any] = []
        for k, v in updates.items():
            if k in ("session_json", "provision_log"):
                sets.append(f"{k} = ?")
                vals.append(json.dumps(v, ensure_ascii=False))
            else:
                sets.append(f"{k} = ?")
                vals.append(v)
        vals.append(account_id)

        with db_conn() as conn:
            conn.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id = ?", vals)
        out = get_account(account_id)
        if not out:
            raise KeyError(account_id)
        return out


def delete_account(account_id: str) -> bool:
    init_db()
    with db_conn() as conn:
        cur = conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    return cur.rowcount > 0


def clear_session_json(account_id: str) -> dict[str, Any]:
    """Ghi đè session_json = {} (không merge — dùng khi reset session tay)."""
    init_db()
    with _ACCOUNT_WRITE_LOCK:
        cur = get_account(account_id)
        if not cur:
            raise KeyError(f"Không có account '{account_id}'")
        now = _now_iso()
        with db_conn() as conn:
            conn.execute(
                "UPDATE accounts SET session_json = ?, updated_at = ? WHERE id = ?",
                ("{}", now, account_id),
            )
        out = get_account(account_id)
        if not out:
            raise KeyError(account_id)
        return out


def _minigame_worth_persist(mg: dict[str, Any]) -> bool:
    """Có dữ liệu mini-game cần ghi DB (token, ws, hoặc CF mini-game.vip)."""
    if mg.get("user_token") or mg.get("ws_token"):
        return True
    cookies = mg.get("cookies")
    return isinstance(cookies, dict) and bool(cookies.get("cf_clearance"))


def _merge_minigame_dict(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base or {})
    for k, v in (patch or {}).items():
        if k == "cookies" and isinstance(v, dict):
            cookies = dict(out.get("cookies") or {})
            cookies.update(v)
            out["cookies"] = cookies
        elif k in ("user_token", "ws_token") and not v and out.get(k):
            continue
        else:
            out[k] = v
    return out


def save_session_runtime(account_id: str, session: dict[str, Any]) -> dict[str, Any]:
    """Lưu cookies/token sau login — merge vào session_json + sync balance."""
    runtime_keys = (
        "cookies",
        "headers",
        "form_token",
        "cek_p",
        "aes_session_key",
        "user_info",
        "login_raw",
        "session_login_at",
        "balance_verified_at",
        "balance_verified_money",
        "linked_banks",
        "withdraw_fast_money",
        "ukey",
        "channel_id",
        "merchant_id",
        "random_remark",
        "default_card_id",
        "minigame",
    )
    patch: dict[str, Any] = {"session_json": {}}
    for k in runtime_keys:
        if k not in session or session[k] is None:
            continue
        if k == "minigame" and isinstance(session[k], dict):
            mg = session[k]
            if not _minigame_worth_persist(mg):
                # Không ghi đè minigame đã có token/CF bằng dict rỗng (balance/ensure_session).
                continue
        patch["session_json"][k] = session[k]

    ui = session.get("user_info") if isinstance(session.get("user_info"), dict) else {}
    money = ui.get("money") or ui.get("total_money")
    if money is not None:
        try:
            patch["balance"] = float(money)
        except (TypeError, ValueError):
            pass

    return update_account(account_id, patch)


def save_accounts_from_session_map(accounts: dict[str, dict]) -> None:
    """Tương thích save_sessions() — ghi từng account."""
    for aid, sess in accounts.items():
        if not get_account(aid):
            create_account(
                {
                    "id": aid,
                    "username": sess.get("username", ""),
                    "password": sess.get("password", ""),
                    "phone": sess.get("phone", ""),
                    "proxy": sess.get("proxy", ""),
                    "session_json": sess,
                }
            )
        else:
            patch = {
                "username": sess.get("username"),
                "password": sess.get("password"),
                "phone": sess.get("phone"),
                "proxy": sess.get("proxy"),
                "fund_password": sess.get("fund_password"),
                "session_json": sess,
            }
            patch = {k: v for k, v in patch.items() if v is not None}
            update_account(aid, patch)
            # Chỉ update_account (đã merge session_json); không gọi save_session_runtime thêm —
            # tránh ghi đè token mini-game bằng bản session cũ trong RAM.


def set_daily_bet_from_mission_api(account_id: str, done_bet_vnd: int | float) -> float:
    """
    Ghi đè daily_bet_total / daily_bet_day từ mission/list level 161 (done_bet_money).
    Khác record_daily_bet (cộng dồn tự tính) — giá trị API là chuẩn tổng cược ngày.
    """
    total = max(0.0, float(done_bet_vnd or 0))
    init_db()
    today = _today_str()
    now = _now_iso()
    with _ACCOUNT_WRITE_LOCK:
        with db_conn() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET daily_bet_total = ?, daily_bet_day = ?, updated_at = ?
                WHERE id = ?
                """,
                (total, today, now, str(account_id)),
            )
    return total


def record_daily_bet(account_id: str, amount_vnd: int | float) -> None:
    """Cộng tổng cược ngày trên cột accounts (tự reset khi sang ngày mới)."""
    amt = float(amount_vnd or 0)
    if amt <= 0:
        return
    init_db()
    today = _today_str()
    with _ACCOUNT_WRITE_LOCK:
        cur = get_account(account_id)
        if not cur:
            return
        day = str(cur.get("daily_bet_day") or "")
        total = float(cur.get("daily_bet_total") or 0)
        if day != today:
            total = 0.0
        new_total = total + amt
        now = _now_iso()
        with db_conn() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET daily_bet_total = ?, daily_bet_day = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_total, today, now, account_id),
            )


def get_daily_bet_totals(account_ids: list[str]) -> dict[str, float]:
    """account_id → tổng cược hôm nay (0 nếu khác ngày)."""
    init_db()
    today = _today_str()
    out = {str(i): 0.0 for i in account_ids if str(i).strip()}
    if not out:
        return out
    placeholders = ",".join("?" * len(out))
    with db_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT id, daily_bet_total, daily_bet_day FROM accounts
            WHERE id IN ({placeholders})
            """,
            list(out.keys()),
        ).fetchall()
    for r in rows:
        aid = str(r["id"])
        if str(r["daily_bet_day"] or "") == today:
            out[aid] = float(r["daily_bet_total"] or 0)
        else:
            out[aid] = 0.0
    return out


def append_provision_log(account_id: str, step: dict[str, Any]) -> list[dict[str, Any]]:
    acc = get_account(account_id)
    if not acc:
        raise KeyError(account_id)
    log = list(acc.get("provision_log") or [])
    step = dict(step)
    step.setdefault("at", _now_iso())
    log.append(step)
    update_account(account_id, {"provision_log": log})
    return log


def public_row(row: dict[str, Any], *, include_secrets: bool = False) -> dict[str, Any]:
    out = {k: row[k] for k in CMS_COLUMNS if k in row}
    out["provision_log"] = row.get("provision_log") or []
    if not include_secrets:
        for k in SENSITIVE_KEYS:
            if out.get(k):
                out[k] = "******"
    return out
