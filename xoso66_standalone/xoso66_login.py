#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI kiểm tra / renew session — logic trong xoso66_session.py."""

from __future__ import annotations

import argparse
import json
import sys

from xoso66_sessions_io import load_sessions
from xoso66_session import (
    bootstrap_prelogin,
    ensure_session,
    get_user_balance,
    persist_session,
    refresh_cloudflare,
    session_health,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="XOSO66 session / login CLI")
    parser.add_argument("-a", "--account", help="account id trong xoso66_sessions.json")
    parser.add_argument("--check", action="store_true", help="kiểm tra session (getBalance)")
    parser.add_argument("--balance", action="store_true", help="gọi getBalance, in số dư")
    parser.add_argument("--force", action="store_true", help="ép login lại")
    parser.add_argument("--dry-bootstrap", action="store_true", help="gọi encryptKey, in form-token")
    parser.add_argument("--refresh-cf", action="store_true", help="tự lấy cf_clearance + cf headers (Playwright)")
    args = parser.parse_args()

    if not args.account:
        parser.print_help()
        return 1

    if args.refresh_cf:
        acc = load_sessions()[args.account]
        report = refresh_cloudflare(acc)
        persist_session(args.account, acc)
        print(json.dumps({"account_id": args.account, **report}, indent=2, ensure_ascii=False))
        return 0 if report.get("ok") else 1

    if args.dry_bootstrap:
        acc = load_sessions()[args.account]
        bootstrap_prelogin(acc)
        print(json.dumps({"form_token": acc.get("form_token"), "cek_p": bool(acc.get("cek_p"))}, indent=2))
        return 0

    if args.check or args.balance:
        acc = load_sessions()[args.account]
        if args.balance:
            bal = get_user_balance(acc)
            persist_session(args.account, acc)
            print(json.dumps({"account_id": args.account, **bal}, indent=2, ensure_ascii=False))
            return 0 if bal.get("ok") else 1
        h = session_health(acc)
        print(json.dumps({"account_id": args.account, **h}, indent=2, ensure_ascii=False))
        return 0 if h.get("ok") else 1

    try:
        acc = ensure_session(args.account, force_login=args.force)
        h = session_health(acc)
        print(json.dumps({"account_id": args.account, "login": "ok", **h}, indent=2, ensure_ascii=False))
        return 0 if h.get("ok") else 1
    except Exception as e:
        print(json.dumps({"account_id": args.account, "ok": False, "error": str(e)}, indent=2, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
