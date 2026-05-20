# -*- coding: utf-8 -*-
"""Migrate xoso66_sessions.json → data/xoso66.db (chạy 1 lần)."""

from __future__ import annotations

import json
import sys

from xoso66_accounts_db import create_account, get_account, init_db
from xoso66_sessions_io import SESSIONS_FILE


def main() -> int:
    if not SESSIONS_FILE.is_file():
        print(f"Không có {SESSIONS_FILE}", file=sys.stderr)
        return 1
    init_db()
    data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
    accounts = data.get("accounts") or []
    n = 0
    for raw in accounts:
        aid = str(raw.get("id") or "").strip()
        if not aid:
            continue
        if get_account(aid):
            print(f"skip {aid} (đã có)")
            continue
        payload = {
            "id": aid,
            "username": raw.get("username", ""),
            "password": raw.get("password", ""),
            "phone": raw.get("phone", ""),
            "account_holder": raw.get("account_holder", ""),
            "fund_password": raw.get("fund_password", ""),
            "proxy": raw.get("proxy", ""),
            "default_card_id": raw.get("default_card_id"),
            "status": "migrated",
            "session_json": raw,
        }
        lb = raw.get("linked_banks")
        if lb:
            payload["bank_code"] = (lb[0] or {}).get("bank_code", "")
            payload["bank_name"] = (lb[0] or {}).get("bank_name", "")
        ui = raw.get("user_info") if isinstance(raw.get("user_info"), dict) else {}
        if ui.get("money"):
            try:
                payload["balance"] = float(ui["money"])
            except (TypeError, ValueError):
                pass
        create_account(payload)
        print(f"ok {aid}")
        n += 1
    from xoso66_accounts_db import DB_PATH

    print(f"Migrated {n} account(s) -> {DB_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
