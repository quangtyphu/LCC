#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bridge cho CMS Node — gọi logic Python (nạp/rút/provision), in JSON stdout.
Không chạy FastAPI :8799. DB/config qua env XOSO66_DB, XOSO66_CONFIG.

  python xoso66_cms_bridge.py deposit --json '{"account_id":"acc1","amount":100000}'
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))
from xoso66_paths import apply_default_env

apply_default_env()

from xoso66_config_util import configure_stdio_utf8

configure_stdio_utf8()


def _emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _fail(msg: str, **extra) -> None:
    _emit({"ok": False, "error": msg, **extra})
    sys.exit(1)


def cmd_deposit(body: dict) -> dict:
    aid = str(body.get("account_id") or "").strip()
    amt = int(body.get("amount") or 0)
    if not aid or amt <= 0:
        raise ValueError("account_id và amount > 0 bắt buộc")
    use_handler = bool(body.get("use_handler"))
    if use_handler:
        from xoso66_auto_deposit import perform_deposit

        return perform_deposit(aid, amt, verbose=False)
    from xoso66_deposit import create_deposit_order

    return create_deposit_order(aid, amt)


def cmd_withdraw(body: dict) -> dict:
    from xoso66_withdraw import withdraw_for_account
    from xoso66_session import ensure_session

    aid = str(body.get("account_id") or "").strip()
    amt = int(body.get("amount") or 0)
    if not aid or amt <= 0:
        raise ValueError("account_id và amount > 0 bắt buộc")
    fund = str(body.get("fund_password") or "").strip()
    card_id = body.get("card_id")
    acc = ensure_session(aid)
    return withdraw_for_account(
        acc,
        amount=amt,
        fund_password=fund or None,
        card_id=card_id,
    )


def cmd_provision(body: dict) -> dict:
    from xoso66_provision import provision_account

    return provision_account(body)


def cmd_refresh_balance(body: dict) -> dict:
    from xoso66_session import ensure_session, refresh_account_balance_to_db

    aid = str(body.get("account_id") or "").strip()
    if not aid:
        raise ValueError("account_id bắt buộc")
    acc = ensure_session(aid, force_login=bool(body.get("force_login")))
    bal = refresh_account_balance_to_db(aid, acc)
    return {"ok": True, "account_id": aid, "balance": bal}


def cmd_sync_payment(body: dict) -> dict:
    from xoso66_payment_history_sync import sync_account_payment_history
    from xoso66_payment_history_db import ORDER_TYPE_DEPOSIT, ORDER_TYPE_WITHDRAW

    aid = str(body.get("account_id") or "").strip()
    days = max(1, int(body.get("days") or 7))
    t = body.get("type")
    if t == 1:
        types = (ORDER_TYPE_DEPOSIT,)
    elif t == 2:
        types = (ORDER_TYPE_WITHDRAW,)
    else:
        types = (ORDER_TYPE_DEPOSIT, ORDER_TYPE_WITHDRAW)
    return sync_account_payment_history(aid, days=days, types=types)


def cmd_refresh_missions(body: dict) -> dict:
    from xoso66_daily_mission_check import refresh_missions_batch

    ids = body.get("account_ids")
    aids = [str(x).strip() for x in (ids or []) if str(x).strip()] if ids else None
    status = str(body.get("status") or "").strip() or None
    return refresh_missions_batch(
        account_ids=aids,
        status_filter=status,
        check_only=bool(body.get("check_only")),
        parallel=max(1, min(32, int(body.get("parallel") or 8))),
        force_login=bool(body.get("force_login")),
    )


def cmd_refresh_vip(body: dict) -> dict:
    from xoso66_vip_check import refresh_vip_batch

    ids = body.get("account_ids")
    aids = [str(x).strip() for x in (ids or []) if str(x).strip()] if ids else None
    status = str(body.get("status") or "").strip() or None
    return refresh_vip_batch(
        account_ids=aids,
        status_filter=status,
        check_only=bool(body.get("check_only")),
        parallel=max(1, min(32, int(body.get("parallel") or 8))),
        force_login=bool(body.get("force_login")),
    )


def cmd_minigame_refresh(body: dict) -> dict:
    from xoso66_minigame_refresh import refresh_minigame_tokens

    aid = str(body.get("account_id") or "").strip()
    return refresh_minigame_tokens(
        {},
        account_id=aid,
        game_key=str(body.get("game_key") or "sicbo"),
        force=bool(body.get("force")),
        ws_only=bool(body.get("ws_only")),
    )


def cmd_check_withdraw(body: dict) -> dict:
    import time

    from xoso66_check_withdraw_history import check_withdraw_history

    key = str(body.get("account_id") or body.get("username") or "").strip()
    if not key:
        raise ValueError("Cần account_id hoặc username")
    since_ms = None
    if body.get("amount"):
        since_min = max(1, int(body.get("since_min") or 60))
        since_ms = int(time.time() * 1000) - since_min * 60 * 1000
    return check_withdraw_history(
        key,
        limit=10,
        days=max(1, int(body.get("days") or 7)),
        amount_vnd=body.get("amount"),
        since_ms=since_ms,
        serial_no=body.get("serial_no"),
        sync_db=True,
        poll=bool(body.get("poll")),
        poll_interval_sec=float(body.get("poll_interval_sec") or 5),
        max_attempts=int(body.get("max_attempts") or 12),
        return_details=True,
        verbose=False,
    )


_ACTIONS = {
    "deposit": cmd_deposit,
    "withdraw": cmd_withdraw,
    "provision": cmd_provision,
    "refresh_balance": cmd_refresh_balance,
    "sync_payment": cmd_sync_payment,
    "refresh_missions": cmd_refresh_missions,
    "refresh_vip": cmd_refresh_vip,
    "minigame_refresh": cmd_minigame_refresh,
    "check_withdraw": cmd_check_withdraw,
}


def main() -> int:
    p = argparse.ArgumentParser(description="XOSO66 CMS bridge (JSON stdout)")
    p.add_argument(
        "action",
        choices=sorted(_ACTIONS.keys()),
        help="Hành động",
    )
    p.add_argument("--json", dest="json_body", default="{}", help="JSON body")
    args = p.parse_args()
    try:
        body = json.loads(args.json_body or "{}")
    except json.JSONDecodeError as e:
        _fail(f"JSON không hợp lệ: {e}")
    if not isinstance(body, dict):
        _fail("JSON body phải là object")
    try:
        fn = _ACTIONS[args.action]
        out = fn(body)
        if not isinstance(out, dict):
            out = {"ok": True, "result": out}
        _emit(out)
        return 0
    except Exception as e:
        _fail(str(e))


if __name__ == "__main__":
    raise SystemExit(main())
