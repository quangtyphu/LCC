# -*- coding: utf-8 -*-
"""Phân biệt callback LC79 (Node :3000) vs XOSO66 (SQLite) — tránh trùng order id."""

from __future__ import annotations

import os
from typing import Any, Literal

import requests

GameTarget = Literal["lc79", "xoso66", "unknown"]

NODE_SERVER_URL = os.environ.get("LC79_NODE_SERVER_URL", "http://127.0.0.1:3000").strip()


def _extract_order_from_api(js: dict) -> dict:
    if not isinstance(js, dict):
        return {}
    d = js.get("data")
    if isinstance(d, dict) and d:
        return d
    if isinstance(d, list) and d and isinstance(d[0], dict):
        return d[0]
    inner = js.get("order") or js.get("depositOrder")
    if isinstance(inner, dict):
        return inner
    if js.get("id") is not None or js.get("username"):
        return js
    return {}


def _order_username(row: dict | None) -> str:
    if not row:
        return ""
    return str(row.get("username") or row.get("Username") or "").strip().lower()


def get_lc79_order(order_id: str | int, transfer_content: str = "") -> dict | None:
    """Đơn nạp trên CMS Node LC79."""
    oid = str(order_id or "").strip()
    if not oid:
        return None
    try:
        r = requests.get(
            f"{NODE_SERVER_URL}/api/deposit-orders/{oid}",
            timeout=5,
        )
        if r.status_code == 200:
            row = _extract_order_from_api(r.json() or {})
            if row and (row.get("id") is not None or row.get("username")):
                return row
    except Exception:
        pass

    tc = (transfer_content or "").strip()
    if not tc:
        return None
    try:
        r2 = requests.get(
            f"{NODE_SERVER_URL}/api/deposit-orders/check-transfer-content",
            params={"transferContent": tc, "exact": "true"},
            timeout=5,
        )
        if not r2.ok:
            return None
        data2 = r2.json() or {}
        orders = data2.get("data") or data2.get("orders") or []
        if not isinstance(orders, list):
            return None
        for o in orders:
            if not isinstance(o, dict):
                continue
            if str(o.get("id")) == oid:
                return o
    except Exception:
        pass
    return None


def get_xoso66_order(order_id: str | int) -> dict | None:
    """Đơn nạp trong SQLite XOSO66."""
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        return None
    try:
        from pathlib import Path
        import sys

        root = Path(__file__).resolve().parent
        xdir = root / "xoso66_standalone"
        if str(xdir) not in sys.path:
            sys.path.insert(0, str(xdir))
        from xoso66_deposit_orders_db import get_deposit_order

        return get_deposit_order(oid)
    except Exception:
        return None


def resolve_callback_game(
    order_id: str | int,
    username: str = "",
    transfer_content: str = "",
) -> GameTarget:
    """
    Xác định đơn thuộc game nào.
    Handler :5000 chỉ forward khi chắc chắn là XOSO66; handler :5001 bỏ qua đơn LC79.
    """
    u = (username or "").strip().lower()
    lc79_row = get_lc79_order(order_id, transfer_content)
    x66_row = get_xoso66_order(order_id)
    has_lc79 = lc79_row is not None
    has_x66 = x66_row is not None

    if has_lc79 and has_x66 and u:
        if u == _order_username(lc79_row):
            return "lc79"
        if u == _order_username(x66_row):
            return "xoso66"

    if has_lc79:
        return "lc79"
    if has_x66:
        return "xoso66"
    return "unknown"
