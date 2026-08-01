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

# Tránh spam force-login khi paymentorderlist liên tục báo hết phiên.
_WITHDRAW_RELOGIN_LAST: dict[str, float] = {}
_WITHDRAW_RELOGIN_LOCK = threading.Lock()
_WITHDRAW_RELOGIN_COOLDOWN_SEC = 90.0


def _withdraw_relogin_cooldown_remaining(account_id: str) -> float:
    aid = str(account_id or "").strip()
    if not aid:
        return 0.0
    with _WITHDRAW_RELOGIN_LOCK:
        last = _WITHDRAW_RELOGIN_LAST.get(aid, 0.0)
    return max(0.0, _WITHDRAW_RELOGIN_COOLDOWN_SEC - (time.time() - last))


def _mark_withdraw_relogin(account_id: str) -> None:
    aid = str(account_id or "").strip()
    if not aid:
        return
    with _WITHDRAW_RELOGIN_LOCK:
        _WITHDRAW_RELOGIN_LAST[aid] = time.time()


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


def _is_session_invalid_api_error(msg: str) -> bool:
    """True nếu paymentorderlist báo hết / sai phiên (cần login lại)."""
    m = str(msg or "").strip().lower()
    if not m:
        return False
    needles = (
        "thông tin phiên không hợp lệ",
        "thong tin phien khong hop le",
        "phiên không hợp lệ",
        "phien khong hop le",
        "hết phiên",
        "het phien",
        "phiên đã hết",
        "phien da het",
        "chưa đăng nhập",
        "chua dang nhap",
        "vui lòng đăng nhập",
        "vui long dang nhap",
        "session invalid",
        "invalid session",
        "not logged in",
        "please login",
    )
    return any(x in m for x in needles)


def _force_relogin_into_session(
    account_id: str,
    session: dict,
    *,
    reason: str = "",
    log_prefix: str | None = None,
) -> dict:
    """Force login → ghi đè session in-place (poll/caller dùng tiếp cookie mới)."""
    from xoso66_accounts_db import username_for_log
    from xoso66_session import ensure_session

    aid = str(account_id or session.get("id") or "").strip()
    if not aid:
        raise ValueError("thiếu account_id để login lại")
    u = username_for_log(aid, session)
    pfx = (log_prefix or "[RÚT]").rstrip()
    hint = str(reason or "phiên không hợp lệ").strip()
    print(f"{pfx} {u}: {hint} — login lại rồi check…", flush=True)
    fresh = ensure_session(aid, force_login=True, ignore_session_ttl=True)
    session.clear()
    session.update(fresh)
    session.setdefault("id", aid)
    return session


def _fetch_withdraw_list_once(
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


def fetch_recent_withdraw_list(
    session: dict,
    *,
    limit: int = 10,
    days: int = 7,
    account_id: str | None = None,
    relogin_on_invalid: bool = True,
    log_prefix: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """
    Kéo paymentorderlist type=2.
    Gặp lỗi phiên (vd. «Thông tin phiên không hợp lệ») → login lại 1 lần rồi fetch lại.
    """
    items, err = _fetch_withdraw_list_once(session, limit=limit, days=days)
    if items or not err or not relogin_on_invalid:
        return items, err
    if not _is_session_invalid_api_error(err):
        return items, err

    aid = str(account_id or session.get("id") or "").strip()
    if not aid:
        return items, err

    cool = _withdraw_relogin_cooldown_remaining(aid)
    if cool > 0:
        return items, f"{err} (chờ login lại ~{int(cool)}s)"

    try:
        _mark_withdraw_relogin(aid)
        _force_relogin_into_session(
            aid,
            session,
            reason=f"API lỗi: {err}",
            log_prefix=log_prefix,
        )
    except Exception as e:
        return [], f"{err} (login lại fail: {e})"

    return _fetch_withdraw_list_once(session, limit=limit, days=days)


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


def _format_withdraw_item_brief(item: dict[str, Any]) -> str:
    sn = str(item.get("serial_no") or "—")
    try:
        amt = int(float(item.get("true_amount") or item.get("amount") or 0))
        amt_s = f"{amt:,}đ"
    except (TypeError, ValueError):
        amt_s = str(item.get("true_amount") or item.get("amount") or "?")
    st = item.get("status")
    sf = str(item.get("status_formatted") or "").strip()
    ct = str(item.get("create_time") or "—")
    st_lbl = f"status={st}" + (f" ({sf})" if sf else "")
    return f"{sn} | {amt_s} | {st_lbl} | {ct}"


def _build_withdraw_poll_snapshot(
    items: list[dict[str, Any]],
    *,
    api_err: str,
    amount_vnd: int,
    since_ms: int,
    serial_no: str | None,
    by_serial: dict[str, dict[str, Any]],
    sync_all: dict[str, Any],
    list_limit: int,
) -> dict[str, Any]:
    briefs = [
        _format_withdraw_item_brief(i)
        for i in items
        if isinstance(i, dict)
    ][: max(1, int(list_limit))]
    snap: dict[str, Any] = {
        "api_error": str(api_err or "").strip() or None,
        "list_count": len(items),
        "items_brief": briefs,
        "sync_upserted": int(sync_all.get("upserted") or 0),
        "sync_new_serials": list(sync_all.get("new_serials") or []),
        "sync_status_changed": list(sync_all.get("status_changed") or []),
    }
    sn = str(serial_no or "").strip()
    if sn:
        item = by_serial.get(sn)
        snap["target_serial"] = sn
        snap["target_in_list"] = item is not None
        if item:
            snap["target_brief"] = _format_withdraw_item_brief(item)
            snap["target_status"] = int(item.get("status") or 0)
            snap["target_status_formatted"] = str(item.get("status_formatted") or "")
        else:
            row = get_payment_order(sn)
            if row:
                snap["target_db_brief"] = (
                    f"DB: status={row.get('status')} "
                    f"({row.get('status_formatted') or ''}) | "
                    f"{row.get('create_time') or '—'}"
                )
    matched = [
        _format_withdraw_item_brief(i)
        for i in items
        if isinstance(i, dict)
        and int(i.get("status") or 0) == SITE_STATUS_SUCCESS
        and _amount_match(i, amount_vnd)
        and _since_ms_ok(i, since_ms)
    ]
    snap["amount_matches_since"] = matched
    return snap


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
    log_prefix: str | None = "[RÚT-POLL]",
) -> dict[str, Any]:
    err = ""
    if items is None:
        items, err = fetch_recent_withdraw_list(
            session,
            limit=list_limit,
            days=days,
            account_id=account_id,
            log_prefix=log_prefix,
        )
    if err and not items:
        return {
            "ok": False,
            "confirmed": False,
            "error": err,
            "poll_snapshot": {
                "api_error": str(err),
                "list_count": 0,
                "items_brief": [],
            },
        }

    sync_all = sync_withdraw_list_to_db(account_id, items)
    sync = sync_all.get("success_sync") or {}
    new_success_serials: list[str] = list(sync.get("new_serials") or [])
    changed_serials: list[str] = list(sync_all.get("status_changed") or [])

    by_serial: dict[str, dict[str, Any]] = {
        str(i.get("serial_no") or "").strip(): i
        for i in items
        if isinstance(i, dict) and str(i.get("serial_no") or "").strip()
    }
    poll_snapshot = _build_withdraw_poll_snapshot(
        items,
        api_err=err,
        amount_vnd=amount_vnd,
        since_ms=since_ms,
        serial_no=serial_no,
        by_serial=by_serial,
        sync_all=sync_all,
        list_limit=list_limit,
    )

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
                "poll_snapshot": poll_snapshot,
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
                "poll_snapshot": poll_snapshot,
            }
        return {
            "ok": True,
            "confirmed": False,
            "sync": sync_all,
            "item": item,
            "hint": f"serial {sn} chưa Hoàn tất",
            "poll_snapshot": poll_snapshot,
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
                "poll_snapshot": poll_snapshot,
            }
        return {
            "ok": True,
            "confirmed": False,
            "sync": sync_all,
            "latest_success": None,
            "hint": "chưa có Hoàn tất mới trong list khớp lệnh này",
            "poll_snapshot": poll_snapshot,
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
        "poll_snapshot": poll_snapshot,
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


def log_withdraw_poll_attempt(
    account_id: str,
    rep: dict[str, Any],
    *,
    attempt: int,
    max_attempts: int,
    amount_vnd: int,
    serial_no: str | None = None,
    log_prefix: str = "[RÚT-POLL]",
) -> None:
    """In chi tiết mỗi lần poll — API trả gì, serial mục tiêu, vì sao chưa confirm."""
    from xoso66_accounts_db import username_for_log

    u = username_for_log(account_id)
    pfx = (log_prefix or "[RÚT-POLL]").rstrip()
    sn_tgt = str(serial_no or rep.get("serial_no") or "").strip()
    head = (
        f"{pfx} {u}: poll {int(attempt)}/{int(max_attempts)} | "
        f"chờ rút {int(amount_vnd):,}đ"
    )
    if sn_tgt:
        head += f" | serial {sn_tgt}"
    lines = [head]

    snap = rep.get("poll_snapshot") if isinstance(rep.get("poll_snapshot"), dict) else {}
    api_err = str(snap.get("api_error") or rep.get("error") or "").strip()
    if api_err and not snap.get("items_brief"):
        lines.append(f"  API lỗi: {api_err}")
    else:
        if api_err:
            lines.append(f"  API cảnh báo: {api_err}")
        n = int(snap.get("list_count") or 0)
        lines.append(f"  API trả {n} lệnh rút (limit {len(snap.get('items_brief') or [])} dòng log):")
        briefs = snap.get("items_brief") or []
        if briefs:
            for row in briefs:
                lines.append(f"    · {row}")
        else:
            lines.append("    · (trống — serial không có trong list)")

        ups = int(snap.get("sync_upserted") or 0)
        new_s = snap.get("sync_new_serials") or []
        chg_s = snap.get("sync_status_changed") or []
        sync_bits: list[str] = []
        if ups:
            sync_bits.append(f"upsert {ups}")
        if new_s:
            sync_bits.append(f"mới {','.join(str(s) for s in new_s)}")
        if chg_s:
            sync_bits.append(f"đổi status {','.join(str(s) for s in chg_s)}")
        if sync_bits:
            lines.append(f"  DB sync: {' | '.join(sync_bits)}")

    if sn_tgt:
        if snap.get("target_in_list"):
            lines.append(f"  serial mục tiêu trong list: {snap.get('target_brief')}")
        elif snap.get("target_db_brief"):
            lines.append(f"  serial mục tiêu không có trong list; {snap['target_db_brief']}")
        else:
            lines.append("  serial mục tiêu: KHÔNG có trong list (DB cũng chưa có)")

    matched = snap.get("amount_matches_since") or []
    if matched:
        lines.append(f"  khớp amount+since ({len(matched)}):")
        for row in matched:
            lines.append(f"    · {row}")
    elif not (rep.get("confirmed") and rep.get("success")):
        lines.append("  khớp amount+since: không có lệnh Hoàn tất")

    if rep.get("confirmed") and rep.get("success"):
        via = str(rep.get("via") or "").strip()
        sn_ok = str(rep.get("serial_no") or sn_tgt or "—")
        lines.append(f"  → OK Hoàn tất | serial {sn_ok}" + (f" | {via}" if via else ""))
    else:
        hint = str(rep.get("hint") or rep.get("error") or "chưa confirm").strip()
        lines.append(f"  → chưa confirm: {hint}")

    print("\n".join(lines), flush=True)


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
    waited = _format_withdraw_poll_waited(attempt, poll_interval_sec)
    sn = str(rep.get("serial_no") or "—")
    via = str(rep.get("via") or "").strip()
    from xoso66_confirm_duration import format_withdraw_poll_attempt_label

    poll_lbl = format_withdraw_poll_attempt_label(attempt, max_attempts)
    pfx = (log_prefix or "[RÚT-POLL]").rstrip()
    msg = (
        f"{pfx} {u}: rút Hoàn tất — {poll_lbl} "
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
    max_attempts: int | None = None,
    list_limit: int = 10,
    days: int = 7,
    serial_no: str | None = None,
    log_prefix: str | None = "[RÚT-POLL]",
) -> dict[str, Any]:
    from xoso66_shutdown import sleep_interruptible, stopping

    total_max = max(1, int(max_attempts)) if max_attempts is not None else withdraw_confirm_poll_max()
    hold_at = min(hold_reward_poll_threshold(), total_max)
    hold_triggered = False
    local_fallback = 0
    last: dict[str, Any] | None = None

    from xoso66_payment_history_db import increment_withdraw_poll_count, peek_withdraw_poll_count

    if peek_withdraw_poll_count(account_id, serial_no) >= total_max:
        err_msg = f"đã hết {total_max} lần poll — chưa thấy Hoàn tất"
        return {
            "ok": False,
            "done": False,
            "success": False,
            "confirmed": False,
            "error": err_msg,
            "last_check": last,
        }

    while True:
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

        local_fallback += 1
        # Tăng trước khi confirm: try_confirm resolve submission → increment sau đó trả 0.
        poll_total = increment_withdraw_poll_count(account_id, serial_no)
        cumulative = poll_total if poll_total > 0 else local_fallback

        chk = try_confirm_withdraw_from_recent_list(
            session,
            account_id=account_id,
            amount_vnd=amount_vnd,
            since_ms=since_ms,
            list_limit=list_limit,
            days=days,
            serial_no=serial_no,
            log_prefix=log_prefix if log_prefix is not None else "[RÚT-POLL]",
        )
        last = chk
        if log_prefix is not None:
            log_withdraw_poll_attempt(
                account_id,
                chk,
                attempt=cumulative,
                max_attempts=total_max,
                amount_vnd=int(amount_vnd),
                serial_no=serial_no,
                log_prefix=log_prefix,
            )
        if chk.get("confirmed") and chk.get("success"):
            chk["attempt"] = cumulative
            chk["max_attempts"] = total_max
            chk["poll_interval_sec"] = float(poll_interval_sec)
            chk["done"] = True
            sn_ok = str(chk.get("serial_no") or serial_no or "").strip()
            if sn_ok:
                from xoso66_payment_history_db import save_withdraw_confirm_poll_attempt

                save_withdraw_confirm_poll_attempt(sn_ok, cumulative)
            if log_prefix is not None:
                log_withdraw_poll_confirmed(
                    account_id,
                    chk,
                    poll_interval_sec=float(poll_interval_sec),
                    max_attempts=total_max,
                    log_prefix=log_prefix,
                )
            return chk

        if cumulative >= hold_at and not hold_triggered:
            hold_triggered = True
            handle_withdraw_poll_exhausted(
                account_id,
                amount_vnd=int(amount_vnd),
                serial_no=serial_no,
                max_attempts=hold_at,
                poll_interval_sec=float(poll_interval_sec),
                error=f"poll {hold_at}/{total_max} chưa Hoàn tất",
                log_prefix=str(log_prefix or "[RÚT-POLL]"),
                total_max=total_max,
            )

        if cumulative >= total_max:
            err_msg = f"hết {total_max} lần poll — chưa thấy Hoàn tất"
            if not hold_triggered:
                handle_withdraw_poll_exhausted(
                    account_id,
                    amount_vnd=int(amount_vnd),
                    serial_no=serial_no,
                    max_attempts=total_max,
                    poll_interval_sec=float(poll_interval_sec),
                    error=err_msg,
                    log_prefix=str(log_prefix or "[RÚT-POLL]"),
                    total_max=total_max,
                )
            return {
                "ok": False,
                "done": False,
                "success": False,
                "confirmed": False,
                "error": err_msg,
                "last_check": last,
            }

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
    """Tổng số lần poll rút (một dãy 1→N, mặc định 20)."""
    cfg = _withdraw_watch_cfg()
    if cfg.get("withdraw_confirm_poll_max") is not None:
        return max(1, int(cfg["withdraw_confirm_poll_max"]))
    return 20


def hold_reward_poll_threshold() -> int:
    """Poll thứ mấy chưa Hoàn tất thì bật hold_reward_above_min_balance=1 (mặc định 5)."""
    cfg = _withdraw_watch_cfg()
    return max(1, int(cfg.get("hold_reward_poll_max", 5)))


def _enable_hold_reward_above_min_balance() -> bool:
    """Ghi hold_reward_above_min_balance=1 vào xoso66_config.json."""
    from xoso66_config_util import CONFIG_PATH, save_user_config_value

    cur = int(_withdraw_watch_cfg().get("hold_reward_above_min_balance", 0))
    if cur == 1:
        return True
    ok = save_user_config_value(("auto_mission_reward", "hold_reward_above_min_balance"), 1)
    if ok:
        print(
            f"[CONFIG] Rút chưa Hoàn tất → bật hold_reward_above_min_balance=1 "
            f"({CONFIG_PATH.name})",
            flush=True,
        )
    else:
        print(
            "[CONFIG] ⚠ không ghi được hold_reward_above_min_balance=1 — kiểm tra tay",
            flush=True,
        )
    return ok


def handle_withdraw_poll_exhausted(
    account_id: str,
    *,
    amount_vnd: int,
    serial_no: str | None = None,
    max_attempts: int,
    poll_interval_sec: float = 60,
    error: str,
    log_prefix: str = "[WITHDRAW-WATCH]",
    total_max: int | None = None,
) -> None:
    """Rút tới ngưỡng poll chưa Hoàn tất → hold_reward_above_min_balance=1."""
    from xoso66_accounts_db import username_for_log
    from xoso66_config_util import CONFIG_PATH
    from xoso66_confirm_duration import format_withdraw_poll_attempt_label

    u = username_for_log(account_id)
    pfx = (log_prefix or "[WITHDRAW-WATCH]").rstrip()
    hold_saved = _enable_hold_reward_above_min_balance()

    mx = int(total_max or max_attempts)
    poll_lbl = format_withdraw_poll_attempt_label(int(max_attempts), mx)
    waited_min = max(1, int(max_attempts)) * max(1.0, float(poll_interval_sec)) / 60.0
    msg = (
        f"{pfx} {u}: ⛔ rút {int(amount_vnd):,}đ — "
        f"{poll_lbl} (~{waited_min:.0f} phút) chưa Hoàn tất"
        + (f" (serial {serial_no})" if serial_no else "")
        + f" — {error}"
    )
    if hold_saved:
        msg += f" → hold_reward_above_min_balance=1 ({CONFIG_PATH.name})"
    else:
        msg += " (⚠ không ghi được hold_reward_above_min_balance=1)"
    print(msg, flush=True)

    if _withdraw_watch_cfg().get("telegram_enabled", True):
        try:
            from xoso66_telegram_notify import notify_auto_mission

            lines = [
                f"⛔ Rút chưa Hoàn tất — {poll_lbl} (~{waited_min:.0f} phút)",
                f"User: {u}",
                f"Số tiền: {int(amount_vnd):,}đ",
                f"Serial: {serial_no or '—'}",
                f"Lý do: {error}",
            ]
            if hold_saved:
                lines.append("→ hold_reward_above_min_balance=1 (bỏ rút/nhận khi số dư > min)")
            notify_auto_mission("\n".join(lines))
        except Exception as te:
            print(f"{pfx} {u}: Telegram lỗi — {te}", flush=True)


def _withdraw_sync_on_ws_cfg() -> dict[str, Any]:
    from xoso66_config_util import load_config

    gw = load_config().get("game_worker")
    return gw if isinstance(gw, dict) else {}


def withdraw_sync_on_ws_open_enabled() -> bool:
    return bool(_withdraw_sync_on_ws_cfg().get("withdraw_sync_on_ws_open", True))


def sync_withdraw_history_on_ws_open(
    session: dict,
    account_id: str,
    *,
    limit: int | None = None,
    days: int | None = None,
    log_prefix: str = "[WS-WD]",
) -> dict[str, Any]:
    """
    Kéo paymentorderlist rút (HTTP) → sync DB.
    Lệnh Hoàn tất mới / đổi trạng thái → upsert_payment_order → cộng device (giống WITHDRAW-WATCH).
    Lỗi API không raise — trả ok=False để WS vẫn mở được.
    """
    from xoso66_accounts_db import username_for_log
    from xoso66_payment_history_db import init_payment_history_tables

    aid = str(account_id or session.get("id") or "").strip()
    cfg = _withdraw_sync_on_ws_cfg()
    lim = max(1, min(50, int(limit if limit is not None else cfg.get("withdraw_sync_list_limit") or 10)))
    d = max(1, int(days if days is not None else cfg.get("withdraw_sync_days") or 7))
    user = username_for_log(aid, session)
    pfx = (log_prefix or "[WS-WD]").rstrip()

    if not aid:
        return {"ok": False, "error": "thiếu account_id"}

    try:
        init_payment_history_tables()
        items, err = fetch_recent_withdraw_list(
            session,
            limit=lim,
            days=d,
            account_id=aid,
            log_prefix=pfx,
        )
        if err and not items:
            print(f"{pfx} {user}: lịch sử rút — {err}", flush=True)
            return {"ok": False, "account_id": aid, "username": user, "error": err, "list": []}

        sync_info: dict[str, Any] = {}
        if items:
            sync_info = sync_withdraw_list_to_db(aid, items)

        ss = sync_info.get("success_sync") or {}
        n_new = int(ss.get("count_new") or 0)
        n_upsert = int(sync_info.get("upserted") or 0)
        n_chg = len(sync_info.get("status_changed") or [])
        new_serials = list(sync_info.get("new_serials") or [])
        new_ok = list(ss.get("new_serials") or [])

        if n_new or n_chg or new_serials:
            print(
                f"{pfx} {user}: sync rút — {len(items)} lệnh site, "
                f"DB {n_upsert} dòng, {n_new} Hoàn tất mới, {n_chg} đổi trạng thái"
                + (f" (serial mới: {', '.join(new_serials[:3])})" if new_serials else ""),
                flush=True,
            )

        return {
            "ok": True,
            "account_id": aid,
            "username": user,
            "list_count": len(items),
            "sync": sync_info,
            "new_success_serials": new_ok,
            "error": err or None,
        }
    except Exception as e:
        print(f"{pfx} {user}: sync rút lỗi — {e}", flush=True)
        return {"ok": False, "account_id": aid, "username": user, "error": str(e)}


_WS_WITHDRAW_SYNC_LAST_TS: dict[str, float] = {}


def maybe_sync_withdraw_history_on_ws_open(
    session: dict,
    account_id: str,
    *,
    log_prefix: str = "[WS-WD]",
) -> dict[str, Any] | None:
    """Theo config + cooldown — gọi trước khi mở WS."""
    if not withdraw_sync_on_ws_open_enabled():
        return None
    aid = str(account_id or "").strip()
    if not aid:
        return None
    cfg = _withdraw_sync_on_ws_cfg()
    cooldown = max(0, int(cfg.get("withdraw_sync_on_ws_cooldown_sec") or 120))
    now = time.time()
    if cooldown > 0:
        last = _WS_WITHDRAW_SYNC_LAST_TS.get(aid, 0.0)
        if now - last < cooldown:
            return None
        _WS_WITHDRAW_SYNC_LAST_TS[aid] = now
    return sync_withdraw_history_on_ws_open(session, aid, log_prefix=log_prefix)


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
    Nền: poll lịch sử rút (một dãy 1→max, mặc định 60s × 20) đến khi Hoàn tất.
    Hoàn tất → sync DB (upsert tự cộng device) + refresh balance acc.
    Poll 5/20 (mặc định) chưa Hoàn tất → hold_reward_above_min_balance=1, tiếp tục tới 20.
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
        )
        if rep.get("confirmed") and rep.get("success"):
            refresh_account_balance_to_db(aid, session, refresh=True)
            return

        err = str(rep.get("error") or "chưa thấy Hoàn tất")
        print(f"{log_prefix} {u}: ⚠ chưa xác nhận Hoàn tất — {err}", flush=True)

    t = threading.Thread(
        target=_worker,
        name=f"xoso66-wd-watch-{str(account_id)[:12]}",
        daemon=True,
    )
    t.start()
    return t
