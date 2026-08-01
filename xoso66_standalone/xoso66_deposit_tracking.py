# -*- coding: utf-8 -*-
"""
Xác nhận nạp: serial_no sau khi tạo lệnh → poll theo serial_no.

Mỗi lần check: lấy 10 lệnh nạp / 7 ngày → ghi Hoàn tất (serial chưa có DB) vào payment_orders.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from xoso66_deposit import list_payment_orders
from xoso66_payment_history_db import (
    ORDER_TYPE_DEPOSIT,
    SITE_STATUS_SUCCESS,
    get_payment_order,
    payment_order_exists,
    sync_deposit_successes_from_list,
)

_poll_order_lock = threading.Lock()
_poll_order_ids: set[int] = set()

DEFAULT_DEPOSIT_LIST_LIMIT = 10
DEPOSIT_LIST_DAYS = 7


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


def deposit_poll_in_progress(order_id: int) -> bool:
    with _poll_order_lock:
        return int(order_id) in _poll_order_ids


_DEPOSIT_TERMINAL_FAIL = frozenset({"Huỷ", "Thất Bại", "Hủy"})


def deposit_order_confirmed(order_id: int) -> bool:
    from xoso66_deposit_orders_db import get_deposit_order

    row = get_deposit_order(int(order_id)) or {}
    return str(row.get("status") or "").strip() == "Thành Công"


def deposit_order_failed(order_id: int) -> str | None:
    """Trả status nếu đơn đã Huỷ/Thất Bại (để poll dừng sớm)."""
    from xoso66_deposit_orders_db import get_deposit_order

    row = get_deposit_order(int(order_id)) or {}
    st = str(row.get("status") or "").strip()
    if st in _DEPOSIT_TERMINAL_FAIL:
        return st
    return None


def _parse_create_time_ms(create_time: str) -> int | None:
    from xoso66_payment_history_db import parse_payment_create_time_ms

    return parse_payment_create_time_ms(create_time)


def _since_ms_ok(item: dict[str, Any], since_ms: int) -> bool:
    """Đơn tạo sau (hoặc gần) thời điểm đặt lệnh nạp."""
    ct_ms = _parse_create_time_ms(str(item.get("create_time") or ""))
    if ct_ms is None:
        return False
    return int(ct_ms) >= int(since_ms) - 5000


def _deposit_items_from_list(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if int(item.get("type") or ORDER_TYPE_DEPOSIT) != ORDER_TYPE_DEPOSIT:
            continue
        serial = str(item.get("serial_no") or "").strip()
        if not serial:
            continue
        out.append(item)
    return out


def fetch_recent_deposit_list(
    session: dict,
    *,
    limit: int = DEFAULT_DEPOSIT_LIST_LIMIT,
) -> tuple[list[dict[str, Any]], str]:
    """10 lệnh nạp gần nhất trong 7 ngày (status=-1: mọi trạng thái)."""
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - DEPOSIT_LIST_DAYS * 24 * 3600 * 1000
    rep = list_payment_orders(
        session=session,
        order_type=ORDER_TYPE_DEPOSIT,
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


def _fetch_and_sync_deposit_list(
    session: dict,
    account_id: str,
    *,
    list_limit: int = DEFAULT_DEPOSIT_LIST_LIMIT,
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    """Lấy list nạp (10/7 ngày) + sync Hoàn tất serial mới vào payment_orders."""
    items, err = fetch_recent_deposit_list(session, limit=list_limit)
    sync = (
        sync_deposit_successes_from_list(account_id, items)
        if items and account_id
        else {"new_serials": [], "updated_serials": [], "count_new": 0}
    )
    return items, sync, err


def capture_latest_deposit_serial_no(
    session: dict,
    *,
    since_ms: int,
    account_id: str = "",
    list_limit: int = DEFAULT_DEPOSIT_LIST_LIMIT,
    retries: int = 3,
    retry_delay_sec: float = 2.0,
) -> dict[str, Any]:
    """
    Sau khi tạo lệnh nạp: lấy serial_no mới nhất (10 lệnh / 7 ngày).
    Đồng thời sync Hoàn tất serial mới vào payment_orders.
    """
    if not since_ms:
        return {"ok": False, "error": "thiếu since_ms"}

    last_err = ""
    attempts = max(1, int(retries))
    for attempt in range(1, attempts + 1):
        if account_id:
            items, sync, err = _fetch_and_sync_deposit_list(
                session, account_id, list_limit=list_limit
            )
        else:
            items, err = fetch_recent_deposit_list(session, limit=list_limit)
            sync = {"new_serials": [], "count_new": 0}

        if err and not items:
            last_err = err
        else:
            candidates: list[tuple[int, str, dict[str, Any]]] = []
            for item in _deposit_items_from_list(items):
                if not _since_ms_ok(item, since_ms):
                    continue
                ct_ms = _parse_create_time_ms(str(item.get("create_time") or "")) or 0
                serial = str(item.get("serial_no") or "").strip()
                candidates.append((ct_ms, serial, item))
            if candidates:
                candidates.sort(key=lambda x: -x[0])
                _ct, serial, item = candidates[0]
                return {
                    "ok": True,
                    "serial_no": serial,
                    "item": item,
                    "attempt": attempt,
                    "sync": sync,
                }
            last_err = "chưa thấy đơn nạp mới trong list"
        if attempt < attempts:
            time.sleep(max(0.5, float(retry_delay_sec)))

    return {
        "ok": False,
        "error": last_err or "không lấy được serial_no",
        "attempts": attempts,
    }


def _item_is_success(item: dict[str, Any]) -> bool:
    return int(item.get("status") or 0) == SITE_STATUS_SUCCESS


def try_confirm_deposit_by_serial_no(
    session: dict,
    *,
    account_id: str,
    serial_no: str,
    list_limit: int = DEFAULT_DEPOSIT_LIST_LIMIT,
) -> dict[str, Any]:
    """
    Lấy 10 lệnh nạp / 7 ngày → sync Hoàn tất serial mới → kiểm tra serial_no.
    """
    sn = str(serial_no or "").strip()
    if not sn:
        return {"ok": False, "confirmed": False, "error": "thiếu serial_no"}

    items, sync, err = _fetch_and_sync_deposit_list(
        session, account_id, list_limit=list_limit
    )
    if err and not items:
        return {"ok": False, "confirmed": False, "error": err, "sync": sync}

    by_serial = {
        str(i.get("serial_no") or "").strip(): i
        for i in items
        if isinstance(i, dict) and str(i.get("serial_no") or "").strip()
    }
    item = by_serial.get(sn)
    if item and _item_is_success(item):
        return {
            "ok": True,
            "confirmed": True,
            "success": True,
            "serial_no": sn,
            "item": item,
            "sync": sync,
            "via": "serial_hoan_tat",
        }

    if payment_order_exists(sn):
        row = get_payment_order(sn) or {}
        if (
            int(row.get("order_type") or 0) == ORDER_TYPE_DEPOSIT
            and int(row.get("status") or 0) == SITE_STATUS_SUCCESS
        ):
            stub = item if isinstance(item, dict) else {
                "serial_no": sn,
                "status": SITE_STATUS_SUCCESS,
                "status_formatted": row.get("status_formatted") or "Hoàn tất",
                "true_amount": row.get("true_amount"),
                "amount": row.get("amount"),
                "create_time": row.get("create_time"),
            }
            return {
                "ok": True,
                "confirmed": True,
                "success": True,
                "serial_no": sn,
                "item": stub,
                "sync": sync,
                "via": "serial_already_in_db",
            }

    hint = f"serial {sn} chưa Hoàn tất"
    if isinstance(item, dict):
        st = item.get("status_formatted") or item.get("status")
        hint = f"serial {sn} status={st!r}"
    return {
        "ok": True,
        "confirmed": False,
        "sync": sync,
        "serial_no": sn,
        "item": item,
        "hint": hint,
    }


def _resolve_poll_serial_no(
    session: dict,
    *,
    serial_no: str,
    order_id: int,
    account_id: str,
    list_limit: int,
) -> tuple[str, str]:
    """Trả (serial_no, error). Thử đọc DB / capture lại nếu thiếu."""
    from xoso66_deposit_orders_db import get_deposit_order, update_deposit_order

    sn = str(serial_no or "").strip()
    row: dict[str, Any] = {}
    if order_id:
        row = get_deposit_order(int(order_id)) or {}
        if not sn:
            sn = str(row.get("serial_no") or "").strip()
        if not account_id:
            account_id = str(row.get("account_id") or "")

    if sn:
        return sn, ""

    since_ms = int(row.get("order_placed_at_ms") or 0)
    if not since_ms:
        return "", "thiếu serial_no và order_placed_at_ms"

    cap = capture_latest_deposit_serial_no(
        session,
        since_ms=since_ms,
        account_id=account_id,
        list_limit=list_limit,
    )
    if not cap.get("ok"):
        return "", str(cap.get("error") or "không lấy được serial_no")
    sn = str(cap.get("serial_no") or "").strip()
    if order_id and sn:
        update_deposit_order(int(order_id), serial_no=sn)
    if not sn:
        return "", "không lấy được serial_no"
    return sn, ""


def poll_deposit_until_confirmed(
    session: dict,
    *,
    account_id: str,
    serial_no: str = "",
    poll_interval_sec: float = 10,
    max_attempts: int = 100,
    list_limit: int = DEFAULT_DEPOSIT_LIST_LIMIT,
    order_id: int | None = None,
    amount_vnd: int | None = None,
    since_ms: int | None = None,
) -> dict[str, Any]:
    from xoso66_shutdown import sleep_interruptible, stopping

    del amount_vnd, since_ms

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
    if oid:
        failed_st = deposit_order_failed(oid)
        if failed_st:
            return {
                "ok": False,
                "done": True,
                "success": False,
                "confirmed": False,
                "cancelled": True,
                "error": f"đơn #{oid} {failed_st}",
                "attempt": 0,
            }

    sn, sn_err = _resolve_poll_serial_no(
        session,
        serial_no=serial_no,
        order_id=oid,
        account_id=account_id,
        list_limit=list_limit,
    )
    if not sn:
        return {
            "ok": False,
            "done": True,
            "success": False,
            "confirmed": False,
            "error": sn_err or "thiếu serial_no",
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
                "serial_no": sn,
                "attempt": attempt,
                "last_check": last,
            }
        if oid:
            failed_st = deposit_order_failed(oid)
            if failed_st:
                return {
                    "ok": False,
                    "done": True,
                    "success": False,
                    "confirmed": False,
                    "cancelled": True,
                    "error": f"đơn #{oid} {failed_st}",
                    "serial_no": sn,
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
                "serial_no": sn,
                "last_check": last,
            }
        chk = try_confirm_deposit_by_serial_no(
            session,
            account_id=account_id,
            serial_no=sn,
            list_limit=list_limit,
        )
        last = chk
        new_n = int((chk.get("sync") or {}).get("count_new") or 0)
        if new_n:
            from xoso66_accounts_db import username_for_log

            user = username_for_log(account_id)
            serials = (chk.get("sync") or {}).get("new_serials") or []
            print(
                f"[DEPOSIT-POLL] [{user}] +{new_n} nạp Hoàn tất vào payment_orders"
                + (f" (serial {serials[0]}…)" if serials else ""),
                flush=True,
            )
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
                    "serial_no": sn,
                    "last_check": last,
                }

    if oid and deposit_order_confirmed(oid):
        return {
            "ok": True,
            "done": True,
            "success": True,
            "confirmed": True,
            "via": "order_already_thanh_cong",
            "serial_no": sn,
            "attempt": max_attempts,
            "last_check": last,
        }

    return {
        "ok": False,
        "done": False,
        "success": False,
        "confirmed": False,
        "serial_no": sn,
        "error": f"hết {max_attempts} lần — serial {sn} chưa Hoàn tất",
        "last_check": last,
    }


def finalize_da_nap_deposit_orders_from_list(
    account_id: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Sau sync payment_orders: cập nhật deposit_orders «Đã Nạp» → «Thành Công»
    khi serial đã Hoàn tất trên site hoặc trong payment_orders.
    """
    from xoso66_deposit_orders_db import finalize_deposit_success, list_deposit_orders_db

    aid = str(account_id).strip()
    if not aid:
        return {"finalized": 0, "order_ids": [], "serials": []}

    by_serial_success: dict[str, dict[str, Any]] = {}
    for item in _deposit_items_from_list(items):
        serial = str(item.get("serial_no") or "").strip()
        if serial and _item_is_success(item):
            by_serial_success[serial] = item

    pending = list_deposit_orders_db(account_id=aid, status="Đã Nạp", limit=200)
    finalized_ids: list[int] = []
    finalized_serials: list[str] = []

    for row in pending.get("list") or []:
        oid = int(row.get("id") or 0)
        if not oid or deposit_order_confirmed(oid):
            continue
        sn = str(row.get("serial_no") or "").strip()
        if not sn:
            continue

        item = by_serial_success.get(sn)
        if not item and payment_order_exists(sn):
            po = get_payment_order(sn) or {}
            if (
                int(po.get("order_type") or 0) == ORDER_TYPE_DEPOSIT
                and int(po.get("status") or 0) == SITE_STATUS_SUCCESS
            ):
                item = {
                    "serial_no": sn,
                    "status": SITE_STATUS_SUCCESS,
                    "status_formatted": po.get("status_formatted") or "Hoàn tất",
                    "true_amount": po.get("true_amount"),
                    "amount": po.get("amount"),
                    "create_time": po.get("create_time"),
                }
        if not item:
            continue

        finalize_deposit_success(
            oid,
            serial_no=sn,
            site_status=int(item.get("status") or SITE_STATUS_SUCCESS),
            site_status_formatted=str(item.get("status_formatted") or "Hoàn tất"),
            game_item=item,
        )
        finalized_ids.append(oid)
        finalized_serials.append(sn)

    if finalized_ids:
        try:
            from xoso66_auto_deposit import release_deposit_order_tracking

            release_deposit_order_tracking(aid)
        except Exception:
            pass
        try:
            from xoso66_auto_deposit import remove_from_deposit_cache

            remove_from_deposit_cache(aid)
        except Exception:
            pass

    return {
        "finalized": len(finalized_ids),
        "order_ids": finalized_ids,
        "serials": finalized_serials,
    }


try_confirm_deposit_from_recent_list = try_confirm_deposit_by_serial_no
