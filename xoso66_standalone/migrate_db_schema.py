#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration DB accounts — chạy khi DB Browser vẫn thấy cột cũ.

  1. Đóng DB Browser (và python main.py nếu đang chạy)
  2. cd xoso66_standalone
  3. python migrate_db_schema.py

Bỏ: channel_id, merchant_id, random_remark
Thêm: daily_bet_total, daily_bet_day, bảng payment_orders (nạp/rút), cột payment_orders.username
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from xoso66_accounts_db import DB_PATH, migrate_accounts_schema


def main() -> int:
    print(f"DB: {DB_PATH}", flush=True)
    if not DB_PATH.is_file():
        print("Chưa có file DB — init_db sẽ tạo schema mới.", flush=True)

    try:
        cols = migrate_accounts_schema()
        from xoso66_payment_history_db import (
            init_payment_history_tables,
            migrate_payment_orders_username,
        )

        init_payment_history_tables()
        migrate_payment_orders_username()
    except Exception as e:
        print(f"LỖI: {e}", flush=True)
        print(
            "→ Đóng DB Browser / main.py rồi chạy lại script này.",
            flush=True,
        )
        return 1

    print("Cột bảng accounts sau migrate:", flush=True)
    for c in cols:
        print(f"  - {c}", flush=True)

    bad = {"channel_id", "merchant_id", "random_remark"} & set(cols)
    good = {"daily_bet_total", "daily_bet_day"} <= set(cols)
    if bad:
        print(f"CẢNH BÁO: vẫn còn cột cũ: {bad}", flush=True)
        return 1
    if not good:
        print("CẢNH BÁO: thiếu daily_bet_total / daily_bet_day", flush=True)
        return 1

    try:
        from xoso66_payment_history_db import _payment_orders_columns
        from xoso66_accounts_db import db_conn

        with db_conn() as c:
            pcols = sorted(_payment_orders_columns(c))
        print("Cột bảng payment_orders:", flush=True)
        for c in pcols:
            print(f"  - {c}", flush=True)
        if "username" not in pcols:
            print("CẢNH BÁO: payment_orders thiếu cột username", flush=True)
            return 1
    except Exception as e:
        print(f"Cảnh báo kiểm tra payment_orders: {e}", flush=True)

    print("OK — schema đã cập nhật. Mở lại DB Browser và bấm Refresh.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
