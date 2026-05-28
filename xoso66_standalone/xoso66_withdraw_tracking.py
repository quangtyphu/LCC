# -*- coding: utf-8 -*-
"""
Xác nhận rút: paymentorderlist type=2 (qua proxy SOCKS5) → sync DB.

Có lệnh Hoàn tất **mới** khớp số tiền + sau lúc gửi lệnh rút → coi rút OK.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from xoso66_deposit import list_payment_orders
from xoso66_payment_history_db import (
    ORDER_TYPE_WITHDRAW,
    SITE_STATUS_SUCCESS,
    get_payment_order,
    sync_withdraw_successes_from_list,
    upsert_payment_orders,
)


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


def _since_ms_ok(item_like: dict[str, Any], since_ms: int) -> bool:
    """Đơn tạo sau (hoặc gần) thời điểm gửi rút."""
    ct_ms = item_like.get("create_time_ms")
    if ct_ms is None:
        ct_ms = _parse_create_time_ms(str(item_like.get("create_time") or ""))
    if ct_ms is None:
        return True
    return int(ct_ms) >= int(since_ms) - 5000


def _db_payment_row_matches_withdraw_attempt(
    row: dict[str, Any],
    *,
    account_id: str,
    amount_vnd: int,
    since_ms: int,
) -> bool:
    if str(row.get("account_id") or "").strip() != str(account_id).strip():
        return False
    if int(row.get("order_type") or 0) != ORDER_TYPE_WITHDRAW:
        return False
    if int(row.get("status") or 0) != SITE_STATUS_SUCCESS:
        return False
    lk = {
        "true_amount": row.get("true_amount"),
        "amount": row.get("amount"),
        "create_time": row.get("create_time"),
        "create_time_ms": row.get("create_time_ms"),
    }
    if not _amount_match(lk, amount_vnd):
        return False
    return _since_ms_ok(lk, since_ms)


def _item_stub_from_payment_row(serial: str, row: dict[str, Any]) -> dict[str, Any]:
    """Trả payload tối giản sau khi xác nhận từ DB."""
    try:
        raw = json.loads(str(row.get("raw_json") or "{}"))
    except Exception:
        raw = {}
    if isinstance(raw, dict) and raw:
        return raw
    return {
        "serial_no": serial,
        "status": int(row.get("status") or 0),
        "true_amount": row.get("true_amount"),
        "amount": row.get("amount"),
        "create_time": row.get("create_time"),
    }


def fetch_recent_withdraw_list(
    session: dict,
    *,
    limit: int = 10,
    days: int = 7,
) -> tuple[list[dict[str, Any]], str]:
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - max(1, int(days)) * 24 * 3600 * 1000
    rep = list_payment_orders(
        session=session,
        order_type=ORDER_TYPE_WITHDRAW,
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


def sync_withdraw_list_to_db(
    account_id: str, items: list[dict[str, Any]]
) -> dict[str, Any]:
    """Upsert mọi trạng thái rút (đang xử lý / hoàn tất / thất bại)."""
    from xoso66_payment_history_db import get_payment_order

    db_status_before: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        serial = str(item.get("serial_no") or "").strip()
        if not serial:
            continue
        row = get_payment_order(serial)
        db_status_before[serial] = int(row["status"]) if row else -1

    n = upsert_payment_orders(account_id, items, ORDER_TYPE_WITHDRAW)
    success_sync = sync_withdraw_successes_from_list(account_id, items)

    status_changed: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        serial = str(item.get("serial_no") or "").strip()
        if not serial:
            continue
        st = int(item.get("status") or 0)
        prev_db = db_status_before.get(serial, -1)
        if prev_db != st:
            status_changed.append(serial)

    new_serials = [s for s, prev in db_status_before.items() if prev == -1]
    return {
        "upserted": n,
        "new_serials": new_serials,
        "status_changed": status_changed,
        "success_sync": success_sync,
    }


def try_confirm_withdraw_from_recent_list(
    session: dict,
    *,
    account_id: str,
    amount_vnd: int,
    since_ms: int,
    list_limit: int = 10,
    days: int = 7,
    serial_no: str | None = None,
    items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    err = ""
    if items is None:
        items, err = fetch_recent_withdraw_list(session, limit=list_limit, days=days)
    if err and not items:
        return {"ok": False, "confirmed": False, "error": err}

    sync_all = sync_withdraw_list_to_db(account_id, items)
    sync = sync_all.get("success_sync") or {}
    new_success_serials: list[str] = list(sync.get("new_serials") or [])
    changed_serials: list[str] = list(sync_all.get("status_changed") or [])

    by_serial: dict[str, dict[str, Any]] = {
        str(i.get("serial_no") or "").strip(): i
        for i in items
        if isinstance(i, dict) and str(i.get("serial_no") or "").strip()
    }

    def _candidate_serials_hint() -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for group in (new_success_serials, changed_serials):
            for s in group:
                k = str(s or "").strip()
                if k and k not in seen:
                    seen.add(k)
                    out.append(k)
        return out

    def _append_candidate(lst: list[tuple[int, str, dict[str, Any]]], s: str, item: dict[str, Any]) -> None:
        if int(item.get("status") or 0) != SITE_STATUS_SUCCESS:
            return
        if not _amount_match(item, amount_vnd):
            return
        ct_ms = _parse_create_time_ms(str(item.get("create_time") or ""))
        if ct_ms is not None and ct_ms < int(since_ms) - 5000:
            return
        lst.append((ct_ms or 0, s, item))

    if serial_no:
        sn = str(serial_no).strip()
        item = by_serial.get(sn)
        if item and int(item.get("status") or 0) == SITE_STATUS_SUCCESS:
            return {
                "ok": True,
                "confirmed": True,
                "success": True,
                "serial_no": sn,
                "item": item,
                "sync": sync_all,
                "via": "serial_no",
            }
        row = get_payment_order(sn)
        if row and _db_payment_row_matches_withdraw_attempt(
            row,
            account_id=account_id,
            amount_vnd=amount_vnd,
            since_ms=since_ms,
        ):
            return {
                "ok": True,
                "confirmed": True,
                "success": True,
                "serial_no": sn,
                "item": item or _item_stub_from_payment_row(sn, row),
                "sync": sync_all,
                "via": "db_serial",
            }
        return {
            "ok": True,
            "confirmed": False,
            "sync": sync_all,
            "item": item,
            "hint": f"serial {sn} chưa Hoàn tất",
        }

    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for s in _candidate_serials_hint():
        item = by_serial.get(s)
        if not item:
            continue
        _append_candidate(candidates, s, item)

    if not candidates:
        latest_ok: dict[str, Any] | None = None
        latest_ms = -1
        for item in items:
            if not isinstance(item, dict):
                continue
            if int(item.get("status") or 0) != SITE_STATUS_SUCCESS:
                continue
            if not _amount_match(item, amount_vnd):
                continue
            ct_ms = _parse_create_time_ms(str(item.get("create_time") or ""))
            if ct_ms is None or ct_ms < int(since_ms) - 5000:
                continue
            if ct_ms > latest_ms:
                latest_ms = ct_ms
                latest_ok = item
        if latest_ok is not None:
            sn_last = str(latest_ok.get("serial_no") or "").strip() or "—"
            return {
                "ok": True,
                "confirmed": True,
                "success": True,
                "serial_no": sn_last,
                "item": latest_ok,
                "sync": sync_all,
                "via": "fingerprint_latest_success",
            }
        return {
            "ok": True,
            "confirmed": False,
            "sync": sync_all,
            "latest_success": None,
            "hint": "chưa có Hoàn tất mới trong list khớp lệnh này",
        }

    candidates.sort(key=lambda x: -x[0])
    _ct, serial, item = candidates[0]
    return {
        "ok": True,
        "confirmed": True,
        "success": True,
        "serial_no": serial,
        "item": item,
        "sync": sync_all,
        "via": "new_success_or_status_change_in_list",
    }


def _format_withdraw_poll_waited(attempt: int, poll_interval_sec: float) -> str:
    """Thời gian từ lúc gửi rút đến khi check thành công (ước lượng)."""
    n = max(1, int(attempt))
    sec = max(0.0, (n - 1) * float(poll_interval_sec))
    if sec >= 120:
        m = sec / 60.0
        if m >= 10:
            return f"~{m:.0f} phút"
        return f"~{m:.1f} phút"
    if sec >= 1:
        return f"~{sec:.0f} giây"
    return "ngay sau khi gửi lệnh"


def log_withdraw_poll_confirmed(
    account_id: str,
    rep: dict[str, Any],
    *,
    poll_interval_sec: float,
    max_attempts: int,
    log_prefix: str = "[RÚT-POLL]",
) -> None:
    """In lần check thành công + thời gian chờ ước lượng (vd. lần 15/20 ~14 phút)."""
    from xoso66_accounts_db import username_for_log

    if not (rep.get("confirmed") and rep.get("success")):
        return
    u = username_for_log(account_id)
    attempt = int(rep.get("attempt") or 0)
    max_a = max(1, int(max_attempts))
    waited = _format_withdraw_poll_waited(attempt, poll_interval_sec)
    sn = str(rep.get("serial_no") or "—")
    via = str(rep.get("via") or "").strip()
    pfx = (log_prefix or "[RÚT-POLL]").rstrip()
    msg = (
        f"{pfx} {u}: rút Hoàn tất — lần check {attempt}/{max_a} "
        f"({waited}, mỗi {float(poll_interval_sec):.0f}s) | serial {sn}"
    )
    if via:
        msg += f" | {via}"
    print(msg, flush=True)


def poll_withdraw_until_confirmed(
    session: dict,
    *,
    account_id: str,
    amount_vnd: int,
    since_ms: int,
    poll_interval_sec: float = 30,
    max_attempts: int = 5,
    list_limit: int = 10,
    days: int = 7,
    serial_no: str | None = None,
    log_prefix: str | None = "[RÚT-POLL]",
    stop_program_on_exhausted: bool = False,
) -> dict[str, Any]:
    from xoso66_shutdown import sleep_interruptible, stopping

    last: dict[str, Any] | None = None
    for attempt in range(1, max(1, int(max_attempts)) + 1):
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
        chk = try_confirm_withdraw_from_recent_list(
            session,
            account_id=account_id,
            amount_vnd=amount_vnd,
            since_ms=since_ms,
            list_limit=list_limit,
            days=days,
            serial_no=serial_no,
        )
        last = chk
        if chk.get("confirmed") and chk.get("success"):
            chk["attempt"] = attempt
            chk["max_attempts"] = int(max_attempts)
            chk["poll_interval_sec"] = float(poll_interval_sec)
            chk["done"] = True
            if log_prefix is not None:
                log_withdraw_poll_confirmed(
                    account_id,
                    chk,
                    poll_interval_sec=float(poll_interval_sec),
                    max_attempts=int(max_attempts),
                    log_prefix=log_prefix,
                )
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

    err_msg = f"hết {max_attempts} lần — chưa thấy Hoàn tất mới khớp lệnh"
    if stop_program_on_exhausted:
        handle_withdraw_poll_exhausted(
            account_id,
            amount_vnd=int(amount_vnd),
            serial_no=serial_no,
            max_attempts=int(max_attempts),
            error=err_msg,
            log_prefix=str(log_prefix or "[RÚT-POLL]"),
        )

    return {
        "ok": False,
        "done": False,
        "success": False,
        "confirmed": False,
        "error": err_msg,
        "last_check": last,
    }


def extract_withdraw_serial(wr: dict[str, Any]) -> str | None:
    data = wr.get("data")
    if isinstance(data, dict):
        for key in ("serial_no", "serialNo", "order_no", "orderNo"):
            v = str(data.get(key) or "").strip()
            if v:
                return v
    raw = wr.get("raw")
    if isinstance(raw, dict):
        inner = raw.get("data")
        if isinstance(inner, dict):
            for key in ("serial_no", "serialNo"):
                v = str(inner.get(key) or "").strip()
                if v:
                    return v
    return None


def _withdraw_watch_cfg() -> dict[str, Any]:
    from xoso66_config_util import load_config

    raw = load_config()
    block = raw.get("auto_mission_reward")
    return block if isinstance(block, dict) else {}


def withdraw_confirm_poll_interval_sec() -> float:
    return float(_withdraw_watch_cfg().get("withdraw_confirm_poll_interval_sec", 60))


def withdraw_confirm_poll_max() -> int:
    return int(_withdraw_watch_cfg().get("withdraw_confirm_poll_max", 20))


def disable_auto_bet_on_withdraw_timeout() -> bool:
    """True → hết lượt poll rút mà chưa Hoàn tất thì tắt auto_bet.enabled trong config."""
    return bool(_withdraw_watch_cfg().get("disable_auto_bet_on_withdraw_timeout", True))


def handle_withdraw_poll_exhausted(
    account_id: str,
    *,
    amount_vnd: int,
    serial_no: str | None = None,
    max_attempts: int,
    error: str,
    log_prefix: str = "[WITHDRAW-WATCH]",
) -> None:
    """Rút quá N lần check chưa Hoàn tất → auto_bet.enabled=false, không cược nữa."""
    from xoso66_accounts_db import username_for_log
    from xoso66_config_util import CONFIG_PATH, save_user_config_value

    if not disable_auto_bet_on_withdraw_timeout():
        return

    u = username_for_log(account_id)
    pfx = (log_prefix or "[WITHDRAW-WATCH]").rstrip()
    saved = save_user_config_value(("auto_bet", "enabled"), False)
    msg = (
        f"{pfx} {u}: ⛔ rút {int(amount_vnd):,}đ — "
        f"{max_attempts} lần check chưa Hoàn tất → tắt auto_bet.enabled"
        + (f" (serial {serial_no})" if serial_no else "")
        + f" — {error}"
    )
    if saved:
        msg += f" (đã ghi {CONFIG_PATH.name})"
    else:
        msg += " (⚠ không ghi được config — kiểm tra tay auto_bet.enabled=false)"
    print(msg, flush=True)

    if _withdraw_watch_cfg().get("telegram_enabled", True):
        try:
            from xoso66_telegram_notify import notify_auto_mission

            notify_auto_mission(
                f"⛔ Tắt auto-bet — rút chưa về sau {max_attempts} lần check\n"
                f"User: {u}\n"
                f"Số tiền: {int(amount_vnd):,}đ\n"
                f"Serial: {serial_no or '—'}\n"
                f"Lý do: {error}\n"
                f"→ auto_bet.enabled=false trong {CONFIG_PATH.name}"
            )
        except Exception as te:
            print(f"{pfx} {u}: Telegram lỗi — {te}", flush=True)


def start_withdraw_confirm_watch(
    account_id: str,
    *,
    amount_vnd: int,
    since_ms: int,
    serial_no: str | None = None,
    log_prefix: str = "[WITHDRAW-WATCH]",
    notify_on_fail: bool = True,
) -> threading.Thread:
    """
    Nền: poll lịch sử rút (mặc định 60s × 20) đến khi Hoàn tất.
    Hoàn tất → sync DB (upsert tự cộng device) + refresh balance acc.
    """

    def _worker() -> None:
        from xoso66_accounts_db import username_for_log
        from xoso66_session import ensure_session, refresh_account_balance_to_db

        aid = str(account_id).strip()
        u = username_for_log(aid)
        interval = withdraw_confirm_poll_interval_sec()
        max_attempts = withdraw_confirm_poll_max()
        try:
            session = ensure_session(aid, force_login=False)
        except Exception as e:
            print(f"{log_prefix} {u}: không mở session — {e}", flush=True)
            return

        print(
            f"{log_prefix} {u}: theo dõi rút {int(amount_vnd):,}đ — "
            f"poll {interval:.0f}s × {max_attempts}"
            + (f" (serial {serial_no})" if serial_no else ""),
            flush=True,
        )
        rep = poll_withdraw_until_confirmed(
            session,
            account_id=aid,
            amount_vnd=int(amount_vnd),
            since_ms=int(since_ms),
            poll_interval_sec=interval,
            max_attempts=max_attempts,
            serial_no=serial_no,
            log_prefix=log_prefix,
            stop_program_on_exhausted=disable_auto_bet_on_withdraw_timeout(),
        )
        if rep.get("confirmed") and rep.get("success"):
            refresh_account_balance_to_db(aid, session, refresh=True)
            return

        err = str(rep.get("error") or "chưa thấy Hoàn tất")
        print(f"{log_prefix} {u}: ⚠ chưa xác nhận Hoàn tất — {err}", flush=True)
        from xoso66_config_util import load_config

        ab = load_config().get("auto_bet")
        auto_bet_off = not (isinstance(ab, dict) and ab.get("enabled"))
        if (
            notify_on_fail
            and not auto_bet_off
            and _withdraw_watch_cfg().get("telegram_enabled", True)
        ):
            try:
                from xoso66_telegram_notify import notify_auto_mission

                notify_auto_mission(
                    f"⚠ Rút chưa Hoàn tất sau {max_attempts} lần check\n"
                    f"User: {u}\n"
                    f"Số tiền: {int(amount_vnd):,}đ\n"
                    f"Serial: {serial_no or '—'}\n"
                    f"Lý do: {err}"
                )
            except Exception as te:
                print(f"{log_prefix} {u}: Telegram lỗi — {te}", flush=True)

    t = threading.Thread(
        target=_worker,
        name=f"xoso66-wd-watch-{str(account_id)[:12]}",
        daemon=True,
    )
    t.start()
    return t
