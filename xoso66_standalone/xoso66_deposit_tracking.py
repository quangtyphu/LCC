# -*- coding: utf-8 -*-
"""
Xác nhận nạp: lấy 10 lệnh nạp gần nhất → so đơn Hoàn tất với DB.

Có lệnh thành công **mới** (vừa lưu DB) khớp số tiền + sau lúc tạo đơn → coi nạp OK.
Không theo dõi serial_no.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from xoso66_deposit import list_payment_orders
from xoso66_payment_history_db import (
    SITE_STATUS_SUCCESS,
    payment_order_exists,
    sync_deposit_successes_from_list,
)

_poll_order_lock = threading.Lock()
_poll_order_ids: set[int] = set()


def try_begin_deposit_poll(order_id: int) -> bool:
    """Một đơn chỉ một luồng poll — tránh WS-POOL + DEPOSIT-POLL chạy song song."""
    oid = int(order_id)
    with _poll_order_lock:
        if oid in _poll_order_ids:
            return False
        _poll_order_ids.add(oid)
        return True


def end_deposit_poll(order_id: int) -> None:
    with _poll_order_lock:
        _poll_order_ids.discard(int(order_id))


def deposit_order_confirmed(order_id: int) -> bool:
    from xoso66_deposit_orders_db import get_deposit_order

    row = get_deposit_order(int(order_id)) or {}
    return str(row.get("status") or "").strip() == "Thành Công"


def _parse_create_time_ms(create_time: str) -> int | None:
    from xoso66_payment_history_db import parse_payment_create_time_ms

    return parse_payment_create_time_ms(create_time)


def _amount_match(item: dict[str, Any], expected: int) -> bool:
    try:
        return int(float(item.get("true_amount") or item.get("amount") or 0)) == int(
            expected
        )
    except (TypeError, ValueError):
        return False


def fetch_recent_deposit_list(
    session: dict,
    *,
    limit: int = 10,
) -> tuple[list[dict[str, Any]], str]:
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 7 * 24 * 3600 * 1000
    rep = list_payment_orders(
        session=session,
        order_type=1,
        status="-1",
        page=1,
        limit=max(1, min(50, int(limit))),
        start_time_ms=start_ms,
        end_time_ms=now_ms,
    )
    if not rep.get("ok"):
        return [], str(rep.get("msg") or rep.get("raw") or "paymentorderlist lỗi")
    batch = rep.get("list") or []
    return batch if isinstance(batch, list) else [], ""


def try_confirm_deposit_from_recent_list(
    session: dict,
    *,
    account_id: str,
    amount_vnd: int,
    since_ms: int,
    list_limit: int = 10,
) -> dict[str, Any]:
    """
    Lấy list nạp (mặc định 10), sync đơn Hoàn tất chưa có DB.
    Nếu có đơn mới lưu khớp amount + thời gian → confirmed.
    """
    items, err = fetch_recent_deposit_list(session, limit=list_limit)
    if err and not items:
        return {"ok": False, "confirmed": False, "error": err}

    sync = sync_deposit_successes_from_list(account_id, items)
    new_serials: list[str] = list(sync.get("new_serials") or [])

    by_serial = {
        str(i.get("serial_no") or "").strip(): i
        for i in items
        if isinstance(i, dict) and str(i.get("serial_no") or "").strip()
    }

    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for serial in new_serials:
        item = by_serial.get(serial)
        if not item:
            continue
        if int(item.get("status") or 0) != SITE_STATUS_SUCCESS:
            continue
        if not _amount_match(item, amount_vnd):
            continue
        ct_ms = _parse_create_time_ms(str(item.get("create_time") or ""))
        if ct_ms is not None and ct_ms < int(since_ms) - 5000:
            continue
        candidates.append((ct_ms or 0, serial, item))

    if not candidates:
        # Luồng poll khác có thể đã sync serial vào DB trước — vẫn coi OK nếu khớp amount + thời gian.
        for item in items:
            if not isinstance(item, dict):
                continue
            if int(item.get("status") or 0) != SITE_STATUS_SUCCESS:
                continue
            if not _amount_match(item, amount_vnd):
                continue
            ct_ms = _parse_create_time_ms(str(item.get("create_time") or ""))
            if ct_ms is not None and ct_ms < int(since_ms) - 5000:
                continue
            serial = str(item.get("serial_no") or "").strip()
            if not serial or not payment_order_exists(serial):
                continue
            return {
                "ok": True,
                "confirmed": True,
                "success": True,
                "serial_no": serial,
                "item": item,
                "sync": sync,
                "via": "already_in_db",
            }

        latest_ok: dict[str, Any] | None = None
        latest_ms = -1
        for item in items:
            if not isinstance(item, dict):
                continue
            if int(item.get("status") or 0) != SITE_STATUS_SUCCESS:
                continue
            ct_ms = _parse_create_time_ms(str(item.get("create_time") or ""))
            if ct_ms is None or ct_ms < int(since_ms) - 5000:
                continue
            if ct_ms > latest_ms:
                latest_ms = ct_ms
                latest_ok = item
        return {
            "ok": True,
            "confirmed": False,
            "sync": sync,
            "latest_success": latest_ok,
            "hint": "chưa có Hoàn tất mới trong DB khớp lệnh này",
        }

    candidates.sort(key=lambda x: -x[0])
    _ct, serial, item = candidates[0]

    return {
        "ok": True,
        "confirmed": True,
        "success": True,
        "serial_no": serial,
        "item": item,
        "sync": sync,
        "via": "new_success_in_list",
    }


def poll_deposit_until_confirmed(
    session: dict,
    *,
    account_id: str,
    amount_vnd: int,
    since_ms: int,
    poll_interval_sec: float = 10,
    max_attempts: int = 100,
    list_limit: int = 10,
    order_id: int | None = None,
) -> dict[str, Any]:
    from xoso66_shutdown import sleep_interruptible, stopping

    oid = int(order_id) if order_id else 0
    if oid and deposit_order_confirmed(oid):
        return {
            "ok": True,
            "done": True,
            "success": True,
            "confirmed": True,
            "via": "order_already_thanh_cong",
            "attempt": 0,
        }

    last: dict[str, Any] | None = None
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        if oid and deposit_order_confirmed(oid):
            return {
                "ok": True,
                "done": True,
                "success": True,
                "confirmed": True,
                "via": "order_already_thanh_cong",
                "attempt": attempt,
                "last_check": last,
            }
        if stopping():
            return {
                "ok": False,
                "done": False,
                "success": False,
                "confirmed": False,
                "cancelled": True,
                "error": "đã hủy (Ctrl+C)",
                "last_check": last,
            }
        chk = try_confirm_deposit_from_recent_list(
            session,
            account_id=account_id,
            amount_vnd=amount_vnd,
            since_ms=since_ms,
            list_limit=list_limit,
        )
        last = chk
        if chk.get("confirmed") and chk.get("success"):
            chk["attempt"] = attempt
            chk["done"] = True
            return chk
        if attempt < max_attempts:
            if not sleep_interruptible(max(1.0, float(poll_interval_sec))):
                return {
                    "ok": False,
                    "done": False,
                    "success": False,
                    "confirmed": False,
                    "cancelled": True,
                    "error": "đã hủy (Ctrl+C)",
                    "last_check": last,
                }

    if oid and deposit_order_confirmed(oid):
        return {
            "ok": True,
            "done": True,
            "success": True,
            "confirmed": True,
            "via": "order_already_thanh_cong",
            "attempt": max_attempts,
            "last_check": last,
        }

    return {
        "ok": False,
        "done": False,
        "success": False,
        "confirmed": False,
        "error": f"hết {max_attempts} lần — chưa thấy Hoàn tất mới trong DB",
        "last_check": last,
    }
