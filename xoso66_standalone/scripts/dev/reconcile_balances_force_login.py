#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Login lại + getBalance mọi acc — sửa DB balance stale (session cũ)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("XOSO66_USE_DB", "1")


def _reconcile_one(account_id: str) -> dict:
    from xoso66_accounts_db import get_account
    from xoso66_session import refresh_account_balance_to_db

    aid = str(account_id).strip()
    row = get_account(aid) or {}
    before = float(row.get("balance") or 0)
    user = str(row.get("username") or aid)
    try:
        rep = refresh_account_balance_to_db(aid, force_relogin=True)
    except Exception as e:
        return {
            "account_id": aid,
            "username": user,
            "ok": False,
            "error": str(e),
            "balance_before": before,
        }
    after = float(rep.get("balance") or 0) if rep.get("ok") else before
    drift = abs(after - before)
    return {
        "account_id": aid,
        "username": user,
        "ok": bool(rep.get("ok")),
        "error": rep.get("error"),
        "balance_before": before,
        "balance_after": after,
        "drift": drift,
        "fixed": drift > 1000,
    }


def main() -> int:
    from xoso66_paths import apply_default_env
    from xoso66_accounts_db import init_db, list_accounts
    from xoso66_proxy import resolve_proxy

    apply_default_env()
    init_db()

    parser = argparse.ArgumentParser(description="Reconcile balance DB — force login mọi acc")
    parser.add_argument("--workers", type=int, default=6, help="parallel workers (default 6)")
    parser.add_argument("--only-drift", action="store_true", help="only print acc with drift >1000")
    parser.add_argument("--account", help="chỉ 1 acc id (vd. acc17789)")
    args = parser.parse_args()

    if args.account:
        ids = [str(args.account).strip()]
    else:
        ids = [
            str(r.get("id") or "").strip()
            for r in list_accounts()
            if str(r.get("id") or "").strip() and resolve_proxy(r)
        ]

    print(f"[reconcile] {len(ids)} acc, {args.workers} workers, force_login=True", flush=True)
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(_reconcile_one, aid): aid for aid in ids}
        for fut in as_completed(futs):
            r = fut.result()
            results.append(r)
            if not r.get("ok"):
                print(f"  FAIL {r.get('username')}: {r.get('error')}", flush=True)
            elif r.get("fixed") or not args.only_drift:
                b0 = int(r.get("balance_before") or 0)
                b1 = int(r.get("balance_after") or 0)
                tag = "FIXED" if r.get("fixed") else "ok"
                if r.get("fixed") or not args.only_drift:
                    print(f"  {tag} {r.get('username')}: {b0:,} -> {b1:,}", flush=True)

    fixed = [r for r in results if r.get("fixed")]
    fail = [r for r in results if not r.get("ok")]
    print(
        f"[reconcile] done: {len(results)} acc, {len(fixed)} drift fixed, {len(fail)} errors",
        flush=True,
    )
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
