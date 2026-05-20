#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Xóa / reset session_json một account trong DB (ô JSON DB Browser thường sửa không được)."""

from __future__ import annotations

import argparse
import sys

from xoso66_accounts_db import clear_session_json, get_account


def main() -> int:
    ap = argparse.ArgumentParser(description="Reset session_json về {}")
    ap.add_argument("-a", "--account", default="acc16", help="account id (mặc định acc16)")
    args = ap.parse_args()
    aid = str(args.account).strip()
    if not get_account(aid):
        print(f"Không có account '{aid}' trong DB", file=sys.stderr)
        return 1
    row = clear_session_json(aid)
    sj = row.get("session_json") or {}
    print(f"OK — {aid}: session_json = {sj!r}")
    print("Lần sau chạy main/login sẽ tạo session mới.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
