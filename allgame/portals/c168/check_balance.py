#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Check số dư C168 theo username (chỉ dùng token đã lưu)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent
_REPO = _ROOT.parent.parent.parent
if str(_ROOT) in sys.path:
    sys.path.remove(str(_ROOT))
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from allgame.db.accounts_db import get_account
from allgame.portals.c168.token import C168TokenChecker
def check_balance(username: str) -> dict[str, Any]:
    user = str(username or "").strip()
    if not user:
        return {"ok": False, "error": "missing_username"}

    account = get_account("c168", user)
    if not account:
        return {"ok": False, "error": "account_not_found", "portal_id": "c168", "username": user}

    checker = C168TokenChecker()
    second = checker._check_by_logout_api(account)  # noqa: SLF001
    return {
        "ok": bool(second.get("ok")),
        "source": "stored_token_only",
        "username": user,
        "status": second.get("status"),
        "code": second.get("code"),
        "balance": second.get("balance"),
        "logout_url": second.get("request", {}).get("logout_url"),
        "error": second.get("error"),
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Check số dư C168 theo username (token đã lưu)")
    parser.add_argument("--username", required=True, help="Username account c168 trong allgame DB")
    args = parser.parse_args()
    out = check_balance(args.username)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

