# -*- coding: utf-8 -*-
"""
Kiểm tra lịch sử rút XOSO66 — paymentorderlist type=2 (qua proxy SOCKS5).

  python xoso66_check_withdraw_history.py bettaixiubnb
  python xoso66_check_withdraw_history.py -a acc1 --limit 10 --days 7
  python xoso66_check_withdraw_history.py user -m 603500 --poll
  python xoso66_check_withdraw_history.py user --serial 1778965217298628140159
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from xoso66_accounts_db import (
    get_account,
    get_account_by_username,
    init_db,
    username_for_log,
)
from xoso66_payment_history_db import (
    ORDER_TYPE_WITHDRAW,
    init_payment_history_tables,
)
from xoso66_session import ensure_session
from xoso66_withdraw_tracking import (
    fetch_recent_withdraw_list,
    poll_withdraw_until_confirmed,
    sync_withdraw_list_to_db,
    try_confirm_withdraw_from_recent_list,
)


def resolve_account_id(account_or_username: str) -> str | None:
    key = str(account_or_username or "").strip()
    if not key:
        return None
    if get_account(key):
        return key
    acc = get_account_by_username(key)
    if acc:
        return str(acc.get("id") or "")
    return None


def _format_item(item: dict[str, Any]) -> str:
    amt = item.get("true_amount") or item.get("amount") or 0
    try:
        amt_s = f"{int(float(amt)):,}"
    except (TypeError, ValueError):
        amt_s = str(amt)
    return (
        f"{item.get('create_time')} | {amt_s}đ | "
        f"{item.get('status_formatted')} | {item.get('serial_no')} | "
        f"{item.get('bank_name_formatted')} {item.get('account_formatted')}"
    )


def check_withdraw_history(
    account_or_username: str,
    *,
    limit: int = 10,
    days: int = 7,
    amount_vnd: int | None = None,
    since_ms: int | None = None,
    serial_no: str | None = None,
    sync_db: bool = True,
    poll: bool = False,
    poll_interval_sec: float = 30,
    max_attempts: int = 5,
    return_details: bool = False,
    verbose: bool = True,
) -> dict[str, Any] | bool:
    aid = resolve_account_id(account_or_username)
    if not aid:
        err = f"không tìm thấy account: {account_or_username!r}"
        if verbose:
            print(f"❌ {err}", flush=True)
        if return_details:
            return {"ok": False, "error": err}
        return False

    init_db()
    init_payment_history_tables()

    try:
        session = ensure_session(aid, force_login=False)
    except Exception as e:
        err = str(e)
        if verbose:
            print(f"❌ [{account_or_username}] session: {err}", flush=True)
        if return_details:
            return {"ok": False, "account_id": aid, "error": err}
        return False

    user = username_for_log(aid)
    if verbose:
        print(
            f"🔎 [{user}] Lịch sử rút — {limit} lệnh / {days} ngày (proxy SOCKS5)",
            flush=True,
        )

    if poll and amount_vnd:
        since = int(since_ms or int(time.time() * 1000))
        rep = poll_withdraw_until_confirmed(
            session,
            account_id=aid,
            amount_vnd=int(amount_vnd),
            since_ms=since,
            poll_interval_sec=poll_interval_sec,
            max_attempts=max_attempts,
            list_limit=limit,
            days=days,
            serial_no=serial_no,
        )
        if verbose:
            if rep.get("confirmed"):
                item = rep.get("item") or {}
                att = int(rep.get("attempt") or 0)
                max_a = int(rep.get("max_attempts") or max_attempts)
                iv = float(rep.get("poll_interval_sec") or poll_interval_sec)
                from xoso66_withdraw_tracking import _format_withdraw_poll_waited

                waited = _format_withdraw_poll_waited(att, iv)
                print(
                    f"💰 [{user}] Rút Hoàn tất — lần check {att}/{max_a} ({waited}) — "
                    f"{_format_item(item)}",
                    flush=True,
                )
            else:
                print(
                    f"⚠️ [{user}] Chưa xác nhận: {rep.get('error') or rep.get('hint')}",
                    flush=True,
                )
        if return_details:
            rep["ok"] = bool(rep.get("confirmed"))
            rep["account_id"] = aid
            rep["username"] = user
            return rep
        return bool(rep.get("confirmed"))

    items, err = fetch_recent_withdraw_list(session, limit=limit, days=days)
    if err and not items:
        if verbose:
            print(f"❌ [{user}] {err}", flush=True)
        if return_details:
            return {"ok": False, "account_id": aid, "error": err, "list": []}
        return False

    sync_info: dict[str, Any] | None = None
    confirm: dict[str, Any] | None = None

    if sync_db and items:
        sync_info = sync_withdraw_list_to_db(aid, items)

    if amount_vnd is not None:
        since = int(since_ms or int(time.time() * 1000) - 3600_000)
        confirm = try_confirm_withdraw_from_recent_list(
            session,
            account_id=aid,
            amount_vnd=int(amount_vnd),
            since_ms=since,
            list_limit=limit,
            days=days,
            serial_no=serial_no,
            items=items,
        )

    if verbose:
        print(f"📋 [{user}] {len(items)} lệnh từ site:", flush=True)
        for item in items:
            if isinstance(item, dict):
                tag = ""
                if confirm and confirm.get("serial_no") == item.get("serial_no"):
                    tag = " ← khớp"
                print(f"   • {_format_item(item)}{tag}", flush=True)
        if sync_info:
            ss = sync_info.get("success_sync") or {}
            n_new = int(ss.get("count_new") or 0)
            n_chg = len(sync_info.get("status_changed") or [])
            if n_new or n_chg:
                print(
                    f"💾 DB: {sync_info.get('upserted')} dòng, "
                    f"{n_new} Hoàn tất mới, {n_chg} đổi trạng thái",
                    flush=True,
                )
        if confirm and confirm.get("confirmed"):
            print(f"✅ [{user}] Xác nhận rút Hoàn tất", flush=True)
        elif confirm and amount_vnd is not None:
            print(f"⏳ [{user}] {confirm.get('hint', 'chưa Hoàn tất')}", flush=True)

    out: dict[str, Any] = {
        "ok": True,
        "account_id": aid,
        "username": user,
        "list": items,
        "sync": sync_info,
        "confirm": confirm,
        "error": err or None,
    }
    if return_details:
        return out
    if confirm is not None:
        return bool(confirm.get("confirmed"))
    return bool(items)


def main() -> int:
    ap = argparse.ArgumentParser(description="Check lịch sử rút XOSO66 (paymentorderlist)")
    ap.add_argument(
        "account",
        nargs="?",
        help="username hoặc account_id (vd. bettaixiubnb)",
    )
    ap.add_argument("-a", "--account-id", dest="account_id", help="account id")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("-m", "--amount", type=int, help="lọc / xác nhận theo số tiền VND")
    ap.add_argument("--serial", help="serial_no cụ thể")
    ap.add_argument("--poll", action="store_true", help="poll đến khi Hoàn tất (-m bắt buộc)")
    ap.add_argument("--poll-interval", type=float, default=30)
    ap.add_argument("--max-attempts", type=int, default=5)
    ap.add_argument("--since-min", type=int, default=60, help="chỉ xét lệnh sau N phút")
    ap.add_argument("--json", action="store_true", help="in JSON thay vì text")
    ap.add_argument("--no-sync", action="store_true", help="không ghi DB")
    args = ap.parse_args()

    key = (args.account or args.account_id or "").strip()
    if not key:
        key = input("Nhập username hoặc account_id: ").strip()
    if not key:
        print("❌ Username / account_id không được để trống", flush=True)
        return 1

    since_ms = None
    if args.amount:
        since_ms = int(time.time() * 1000) - max(1, int(args.since_min)) * 60 * 1000

    rep = check_withdraw_history(
        key,
        limit=args.limit,
        days=args.days,
        amount_vnd=args.amount,
        since_ms=since_ms,
        serial_no=args.serial,
        sync_db=not args.no_sync,
        poll=args.poll,
        poll_interval_sec=args.poll_interval,
        max_attempts=args.max_attempts,
        return_details=True,
        verbose=not args.json,
    )

    if args.json:
        print(json.dumps(rep, indent=2, ensure_ascii=False, default=str), flush=True)

    if not rep.get("ok"):
        return 1
    if args.poll and args.amount:
        return 0 if rep.get("confirmed") else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
