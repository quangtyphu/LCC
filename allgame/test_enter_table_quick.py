#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test nhanh: nhập username, chạy tới khi vào được bàn Game B."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent
_REPO = _ROOT.parent
os.chdir(_ROOT)
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from allgame.config_util import load_config
from allgame.db.accounts_db import get_account
from allgame.portals.c168.open_chrome_token import ensure_chrome_for_username
from allgame.vendor.ws_connector import connect_vendor_ws


def _extract_session_info(row: dict[str, Any]) -> dict[str, Any]:
    sample = str(row.get("ws_table_session_sample") or "")
    if not sample:
        return {}
    out: dict[str, Any] = {}
    m_table = re.search(r'"tableID"\s*:\s*(\d+)', sample)
    m_shoe = re.search(r'"gameShoe"\s*:\s*(\d+)', sample)
    m_round = re.search(r'"gameRound"\s*:\s*(\d+)', sample)
    if m_table:
        out["tableID"] = int(m_table.group(1))
    if m_shoe:
        out["gameShoe"] = int(m_shoe.group(1))
    if m_round:
        out["gameRound"] = int(m_round.group(1))
    if out:
        out["sample"] = sample
    return out


def run_quick_enter(username: str, retries: int = 2, sleep_sec: float = 1.5) -> dict[str, Any]:
    user = str(username or "").strip()
    if not user:
        return {"ok": False, "error": "missing_username"}

    account = get_account("c168", user)
    if not account:
        return {"ok": False, "error": "account_not_found", "portal_id": "c168", "username": user}

    chrome = ensure_chrome_for_username(user, account=account)
    if not chrome.get("ok"):
        return {"ok": False, "error": "ensure_chrome_failed", "detail": chrome}

    cfg = load_config()
    attempts: list[dict[str, Any]] = []
    total = max(1, int(retries))
    for i in range(1, total + 1):
        out = connect_vendor_ws(account, chrome=chrome, cfg=cfg)
        out = out if isinstance(out, dict) else {"ok": False, "error": "bad_result_type"}
        out["session_info"] = _extract_session_info(out)
        out["attempt"] = i
        attempts.append(out)
        if bool(out.get("ready_to_bet")):
            return {
                "ok": True,
                "username": user,
                "attempt": i,
                "result": out,
                "attempts": attempts,
            }
        if i < total:
            time.sleep(max(0.5, float(sleep_sec)))

    return {
        "ok": False,
        "username": user,
        "error": "enter_table_failed",
        "attempts": attempts,
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    os.environ.setdefault("PYTHONUNBUFFERED", "1")

    ap = argparse.ArgumentParser(description="Test nhanh vào bàn Game B theo username")
    ap.add_argument("--username", required=True, help="Username c168 trong allgame DB")
    ap.add_argument("--retries", type=int, default=2, help="Số lần thử vào bàn")
    ap.add_argument("--sleep", type=float, default=1.5, help="Nghỉ giữa các lần thử (giây)")
    ap.add_argument("--table-name", default="", help="Tên bàn override, vd C05")
    ap.add_argument("--table-id", type=int, default=0, help="ID bàn override, vd 1005")
    args = ap.parse_args()
    out = run_quick_enter(args.username, retries=args.retries, sleep_sec=args.sleep)
    if args.table_name or args.table_id:
        # rerun with table override on demand
        user = str(args.username or "").strip()
        account = get_account("c168", user)
        if not account:
            out = {"ok": False, "error": "account_not_found", "username": user}
        else:
            chrome = ensure_chrome_for_username(user, account=account)
            if not chrome.get("ok"):
                out = {"ok": False, "error": "ensure_chrome_failed", "detail": chrome}
            else:
                cfg = load_config()
                vendor = dict(cfg.get("vendor") or {})
                if args.table_name:
                    vendor["table_name"] = str(args.table_name).strip()
                if int(args.table_id or 0) > 0:
                    vendor["table_id"] = int(args.table_id)
                cfg["vendor"] = vendor
                attempts: list[dict[str, Any]] = []
                total = max(1, int(args.retries))
                for i in range(1, total + 1):
                    r = connect_vendor_ws(account, chrome=chrome, cfg=cfg)
                    r = r if isinstance(r, dict) else {"ok": False, "error": "bad_result_type"}
                    r["session_info"] = _extract_session_info(r)
                    r["attempt"] = i
                    attempts.append(r)
                    if bool(r.get("ready_to_bet")):
                        out = {"ok": True, "username": user, "attempt": i, "result": r, "attempts": attempts}
                        break
                    if i < total:
                        time.sleep(max(0.5, float(args.sleep)))
                else:
                    out = {"ok": False, "username": user, "error": "enter_table_failed", "attempts": attempts}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

