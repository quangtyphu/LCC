# -*- coding: utf-8 -*-
"""
Số dư thiết bị ngân hàng — DB LC79/CMS (game_data.db → device_balances).

- App VPBank / CMS form: PUT ghi **toàn bộ** số dư trên máy (update_device_balance).
- Rút Hoàn tất ghi `payment_orders` (`upsert_payment_order`) → tự **cộng** số tiền rút vào device.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

_ROOT = Path(__file__).resolve().parent
_LC79_ROOT = _ROOT.parent


def default_game_data_db_path() -> Path:
    """.../Documents/CMS/game_data.db — cùng LC79/Banking."""
    env = str(os.environ.get("GAME_DATA_DB") or os.environ.get("DEVICE_BALANCE_DB") or "").strip()
    if env:
        return Path(env)
    block = _cfg()
    cfg_path = str(block.get("game_data_db") or block.get("db_path") or "").strip()
    if cfg_path:
        return Path(cfg_path)
    return _LC79_ROOT.parent / "CMS" / "game_data.db"


def _cfg() -> dict[str, Any]:
    from xoso66_config_util import load_config

    raw = load_config()
    block = raw.get("device_balance")
    if isinstance(block, dict):
        return block
    ad = raw.get("auto_deposit")
    if isinstance(ad, dict):
        tp = str(ad.get("third_party_url") or "").strip()
        if tp:
            base = tp.rsplit("/api/", 1)[0].rstrip("/")
            return {"banking_api_url": base}
    return {}


def _banking_base_url() -> str:
    block = _cfg()
    url = str(
        block.get("banking_api_url")
        or os.environ.get("XOSO66_BANKING_API_URL")
        or os.environ.get("LC79_BANKING_API_URL")
        or ""
    ).strip()
    if url:
        return url.rstrip("/")
    return "http://127.0.0.1:8888"


def _cms_base_url() -> str:
    block = _cfg()
    return str(
        block.get("cms_api_url")
        or os.environ.get("LC79_NODE_SERVER_URL")
        or os.environ.get("CMS_PROXY_URL")
        or "http://127.0.0.1:3000"
    ).rstrip("/")


def device_name_for_account(account_id: str) -> str:
    from xoso66_accounts_db import get_account

    row = get_account(str(account_id).strip()) or {}
    return str(row.get("device") or "").strip()


def withdraw_amount_from_item(item: dict[str, Any] | None) -> int | None:
    if not item:
        return None
    try:
        return int(float(item.get("true_amount") or item.get("amount") or 0))
    except (TypeError, ValueError):
        return None


def _get_device_balance_sqlite(db_path: Path, device: str) -> int | None:
    conn = sqlite3.connect(str(db_path), timeout=15)
    try:
        cur = conn.cursor()
        cur.execute("SELECT balance FROM device_balances WHERE device = ?", (device,))
        row = cur.fetchone()
        if not row:
            return None
        return int(row[0] or 0)
    finally:
        conn.close()


def _get_device_balance_http(base_url: str, device: str) -> int | None:
    url = f"{base_url.rstrip('/')}/api/device-balances/{quote(device, safe='')}"
    r = requests.get(url, timeout=15)
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        return None
    try:
        payload = r.json()
    except Exception:
        return None
    if isinstance(payload, dict):
        return int(payload.get("balance") or 0)
    return None


def _add_to_device_balance_sqlite(
    db_path: Path, device: str, amount: int
) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE device_balances
            SET balance = balance + ?, updatedAt = datetime('now')
            WHERE device = ?
            """,
            (amount, device),
        )
        inserted = cur.rowcount == 0
        if inserted:
            try:
                cur.execute(
                    """
                    INSERT INTO device_balances (
                        device, balance, bank, username, accountNumber, accountHolder, updatedAt
                    ) VALUES (?, ?, '', '', '', '', datetime('now'))
                    """,
                    (device, amount),
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                cur.execute(
                    """
                    UPDATE device_balances
                    SET balance = balance + ?, updatedAt = datetime('now')
                    WHERE device = ?
                    """,
                    (amount, device),
                )
                inserted = False
        conn.commit()
        cur.execute("SELECT balance FROM device_balances WHERE device = ?", (device,))
        row = cur.fetchone()
        if not row:
            return {"ok": False, "error": "không đọc lại device sau cộng"}
        new_bal = int(row["balance"] or 0)
        return {
            "ok": True,
            "via": "sqlite",
            "device": device,
            "balance": new_bal,
            "added": amount,
            "inserted": inserted,
            "db_path": str(db_path),
        }
    finally:
        conn.close()


def _add_to_device_balance_http(base_url: str, device: str, amount: int) -> dict[str, Any]:
    current = _get_device_balance_http(base_url, device)
    if current is None:
        new_bal = amount
    else:
        new_bal = current + amount
    return _update_device_balance_http(base_url, device, new_bal, added=amount)


def _update_device_balance_sqlite(db_path: Path, device: str, balance: int) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE device_balances
            SET balance = ?, updatedAt = datetime('now')
            WHERE device = ?
            """,
            (balance, device),
        )
        inserted = cur.rowcount == 0
        if inserted:
            try:
                cur.execute(
                    """
                    INSERT INTO device_balances (
                        device, balance, bank, username, accountNumber, accountHolder, updatedAt
                    ) VALUES (?, ?, '', '', '', '', datetime('now'))
                    """,
                    (device, balance),
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                cur.execute(
                    """
                    UPDATE device_balances
                    SET balance = ?, updatedAt = datetime('now')
                    WHERE device = ?
                    """,
                    (balance, device),
                )
                inserted = False
        conn.commit()
        cur.execute("SELECT * FROM device_balances WHERE device = ?", (device,))
        row = cur.fetchone()
        if not row:
            return {"ok": False, "error": "không đọc lại device sau ghi"}
        return {
            "ok": True,
            "via": "sqlite",
            "device": device,
            "balance": int(row["balance"] or 0),
            "inserted": inserted,
            "db_path": str(db_path),
        }
    finally:
        conn.close()


def _update_device_balance_http(
    base_url: str, device: str, balance: int, *, added: int | None = None
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/device-balances/{quote(device, safe='')}"
    r = requests.put(url, json={"balance": balance}, timeout=15)
    if r.status_code not in (200, 201):
        try:
            body = r.json()
        except Exception:
            body = r.text[:300]
        return {
            "ok": False,
            "error": f"HTTP {r.status_code}",
            "detail": body,
            "url": url,
        }
    try:
        payload = r.json()
    except Exception:
        payload = {}
    rep: dict[str, Any] = {
        "ok": True,
        "via": "http",
        "device": device,
        "balance": int((payload or {}).get("balance") or balance),
        "url": url,
    }
    if added is not None:
        rep["added"] = added
    return rep


def update_device_balance(
    device: str,
    balance_vnd: int | float,
    *,
    log_prefix: str = "[DEVICE-BAL]",
) -> dict[str, Any]:
    """Ghi đè số dư thiết bị (app VPBank / form CMS)."""
    dev = str(device or "").strip()
    if not dev:
        return {"ok": False, "skipped": True, "reason": "thiếu tên thiết bị"}
    try:
        bal = int(float(balance_vnd or 0))
    except (TypeError, ValueError):
        return {"ok": False, "error": "balance không hợp lệ"}

    db_path = default_game_data_db_path()
    if db_path.is_file():
        try:
            rep = _update_device_balance_sqlite(db_path, dev, bal)
            if rep.get("ok"):
                print(
                    f"{log_prefix} {dev}: set balance={bal:,}đ (SQLite {db_path.name})",
                    flush=True,
                )
            return rep
        except Exception as e:
            print(f"{log_prefix} SQLite lỗi: {e}", flush=True)

    for base, label in ((_banking_base_url(), "Banking"), (_cms_base_url(), "CMS")):
        try:
            rep = _update_device_balance_http(base, dev, bal)
            if rep.get("ok"):
                print(f"{log_prefix} {dev}: set balance={bal:,}đ ({label})", flush=True)
                return rep
        except Exception as e:
            print(f"{log_prefix} {label} lỗi: {e}", flush=True)

    return {"ok": False, "error": "không ghi được device_balances"}


def log_device_credit_result(
    rep: dict[str, Any],
    *,
    log_prefix: str = "[DEVICE-BAL]",
    username: str = "",
    serial_no: str = "",
) -> None:
    """In log rõ khi cộng / bỏ qua / lỗi (tránh im lặng)."""
    tag = f" {username}" if username else ""
    sn = f" serial={serial_no}" if serial_no else ""
    if rep.get("skipped"):
        print(
            f"{log_prefix}{tag}: bỏ qua device — {rep.get('reason', '?')}{sn}",
            flush=True,
        )
        return
    if rep.get("ok"):
        dev = rep.get("device") or (rep.get("device_sync") or {}).get("device") or "?"
        added = rep.get("withdraw_amount") or rep.get("added") or "?"
        bal = rep.get("balance") or (rep.get("device_sync") or {}).get("balance")
        bal_s = f"{int(bal):,}đ" if bal is not None else "—"
        print(
            f"{log_prefix}{tag}: ✅ device {dev} +{added}đ → {bal_s}{sn}",
            flush=True,
        )
        return
    err = rep.get("error") or (rep.get("device_sync") or {}).get("error") or "?"
    print(f"{log_prefix}{tag}: ❌ device — {err}{sn}", flush=True)


def add_to_device_balance(
    device: str,
    amount_vnd: int | float,
    *,
    log_prefix: str = "[DEVICE-BAL]",
) -> dict[str, Any]:
    """Cộng tiền rút (hoặc tiền về TK) vào số dư thiết bị ngân hàng."""
    dev = str(device or "").strip()
    if not dev:
        return {"ok": False, "skipped": True, "reason": "thiếu tên thiết bị"}
    try:
        amt = int(float(amount_vnd or 0))
    except (TypeError, ValueError):
        return {"ok": False, "error": "số tiền không hợp lệ"}
    if amt <= 0:
        return {"ok": False, "error": "số tiền phải > 0"}

    db_path = default_game_data_db_path()
    if db_path.is_file():
        try:
            before = _get_device_balance_sqlite(db_path, dev)
            rep = _add_to_device_balance_sqlite(db_path, dev, amt)
            if rep.get("ok"):
                prev_s = f"{before:,}đ" if before is not None else "—"
                print(
                    f"{log_prefix} {dev}: +{amt:,}đ rút → {prev_s} → {rep['balance']:,}đ "
                    f"(SQLite {db_path.name})",
                    flush=True,
                )
            if not rep.get("ok"):
                print(
                    f"{log_prefix} {dev}: SQLite không cộng — {rep.get('error', '?')}",
                    flush=True,
                )
            return rep
        except Exception as e:
            print(f"{log_prefix} SQLite lỗi: {e}", flush=True)

    for base, label in ((_banking_base_url(), "Banking"), (_cms_base_url(), "CMS")):
        try:
            before = _get_device_balance_http(base, dev)
            rep = _add_to_device_balance_http(base, dev, amt)
            if rep.get("ok"):
                prev_s = f"{before:,}đ" if before is not None else "—"
                print(
                    f"{log_prefix} {dev}: +{amt:,}đ rút → {prev_s} → {rep['balance']:,}đ ({label})",
                    flush=True,
                )
                return rep
            print(
                f"{log_prefix} {dev}: {label} không cộng — {rep.get('error', '?')}",
                flush=True,
            )
        except Exception as e:
            print(f"{log_prefix} {label} lỗi: {e}", flush=True)

    db_path = default_game_data_db_path()
    hint = f" (không thấy DB: {db_path})" if not db_path.is_file() else ""
    return {"ok": False, "error": f"không cộng được device_balances{hint}"}


def credit_device_for_account_withdraw(
    account_id: str,
    withdraw_amount_vnd: int | float,
    *,
    log_prefix: str = "[DEVICE-BAL]",
) -> dict[str, Any]:
    """accounts.device → cộng số tiền rút vào device_balances."""
    aid = str(account_id or "").strip()
    dev = device_name_for_account(aid)
    if not dev:
        return {"ok": False, "skipped": True, "reason": "acc không có cột device"}
    rep = add_to_device_balance(dev, withdraw_amount_vnd, log_prefix=log_prefix)
    rep["account_id"] = aid
    rep["device"] = dev
    return rep


def credit_device_on_withdraw_saved(
    account_id: str,
    item: dict[str, Any],
    *,
    log_prefix: str = "[DEVICE-BAL]",
) -> dict[str, Any]:
    """
    Gọi từ upsert_payment_order: lệnh rút Hoàn tất vừa ghi DB → cộng device.
    """
    from xoso66_accounts_db import username_for_log
    from xoso66_payment_history_db import mark_device_balance_credited

    aid = str(account_id).strip()
    user = username_for_log(aid)
    serial = str(item.get("serial_no") or "").strip()
    amt = withdraw_amount_from_item(item)
    if amt is None:
        rep = {"ok": False, "error": "thiếu số tiền rút", "serial_no": serial}
        log_device_credit_result(rep, log_prefix=log_prefix, username=user, serial_no=serial)
        return rep
    try:
        amt_i = int(float(amt))
    except (TypeError, ValueError):
        rep = {"ok": False, "error": "số tiền rút không hợp lệ", "serial_no": serial}
        log_device_credit_result(rep, log_prefix=log_prefix, username=user, serial_no=serial)
        return rep
    if amt_i <= 0:
        rep = {"ok": False, "error": "số tiền rút phải > 0", "serial_no": serial}
        log_device_credit_result(rep, log_prefix=log_prefix, username=user, serial_no=serial)
        return rep

    dev_rep = credit_device_for_account_withdraw(aid, amt_i, log_prefix=log_prefix)
    rep: dict[str, Any] = {
        "ok": bool(dev_rep.get("ok")),
        "device_sync": dev_rep,
        "withdraw_amount": amt_i,
        "serial_no": serial,
    }
    if rep.get("ok") and serial:
        dev = dev_rep.get("device") or device_name_for_account(aid)
        mark_device_balance_credited(
            serial,
            account_id=aid,
            amount_vnd=amt_i,
            device=str(dev or ""),
        )
    log_device_credit_result(
        rep, log_prefix=log_prefix, username=user, serial_no=serial
    )
    return rep


def after_withdraw_confirmed(
    account_id: str,
    *,
    withdraw_amount_vnd: int | float | None = None,
    item: dict[str, Any] | None = None,
    session: dict | None = None,  # noqa: ARG001
    balance_vnd: int | float | None = None,  # noqa: ARG001
    log_prefix: str = "[AUTO-MISSION]",
) -> dict[str, Any]:
    """Tương thích cũ — nên đi qua upsert_payment_order."""
    row = dict(item or {})
    if withdraw_amount_vnd is not None:
        row.setdefault("true_amount", withdraw_amount_vnd)
        row.setdefault("amount", withdraw_amount_vnd)
    row.setdefault("status", 1)
    row.setdefault("type", 2)
    return credit_device_on_withdraw_saved(
        account_id, row, log_prefix=log_prefix
    )
