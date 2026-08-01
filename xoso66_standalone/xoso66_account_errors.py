# -*- coding: utf-8 -*-
"""
Lỗi site nghiêm trọng → đổi status acc sang «Lỗi» và dừng auto-mission.

Nhận diện (không phân biệt hoa thường):
  - Thao tác trên hệ thống của bạn lặp lại quá thường xuyên

Lưu ý: «Mã xác nhận không chính xác» (captcha) không còn coi là fatal —
login/register tự giải Capsolver và retry.
"""

from __future__ import annotations

from typing import Any

_FATAL_MSG_MARKERS: tuple[str, ...] = (
    "thao tác trên hệ thống của bạn lặp lại quá thường xuyên",
)


def is_fatal_system_error_msg(msg: str) -> bool:
    m = str(msg or "").strip().lower()
    if not m:
        return False
    return any(marker in m for marker in _FATAL_MSG_MARKERS)


def _resolve_account_id(account_id: str = "", session: dict[str, Any] | None = None) -> str:
    aid = str(account_id or "").strip()
    if aid:
        return aid
    if not session:
        return ""
    aid = str(session.get("id") or session.get("_balance_log_account_id") or "").strip()
    if aid:
        return aid
    u = str(session.get("username") or "").strip()
    if not u:
        return ""
    from xoso66_accounts_db import get_account_by_username

    row = get_account_by_username(u)
    if not row:
        return ""
    return str(row.get("id") or "").strip()


def _cancel_mission_queue(aid: str) -> None:
    try:
        from xoso66_auto_mission_reward import cancel_mission_claim_queue

        cancel_mission_claim_queue(aid)
    except Exception:
        pass


def maybe_mark_account_loi(
    account_id: str,
    msg: str,
    *,
    source: str = "",
    session: dict[str, Any] | None = None,
) -> bool:
    """
    Nếu msg là lỗi hệ thống nghiêm trọng → status «Lỗi», xóa hàng đợi auto-mission.
    Trả True nếu đã xử lý (kể cả acc đã là Lỗi trước đó).
    """
    if not is_fatal_system_error_msg(msg):
        return False
    from xoso66_accounts_db import (
        STATUS_LOI,
        get_account,
        set_account_status,
        username_for_log,
    )

    aid = _resolve_account_id(account_id, session)
    if not aid:
        return False

    row = get_account(aid) or {}
    u = username_for_log(aid, row)
    short_msg = str(msg).strip()[:160]
    reason = f"{source}: {short_msg}" if source else short_msg

    if str(row.get("status") or "").strip() != STATUS_LOI:
        set_account_status(aid, STATUS_LOI, reason=reason)
    else:
        print(
            f"[ACCOUNT] {u}: lỗi hệ thống (đã Lỗi) — {short_msg}",
            flush=True,
        )

    _cancel_mission_queue(aid)
    return True


def maybe_mark_account_loi_from_session(
    session: dict[str, Any],
    msg: str,
    *,
    source: str = "",
    account_id: str = "",
) -> bool:
    return maybe_mark_account_loi(
        account_id,
        msg,
        source=source,
        session=session,
    )


def maybe_mark_account_loi_from_api(
    session: dict[str, Any],
    body: Any,
    *,
    source: str = "",
    account_id: str = "",
) -> bool:
    """Kiểm tra body JSON API (code != 1) — trả True nếu đã đánh Lỗi."""
    if not isinstance(body, dict):
        return False
    if body.get("code") == 1:
        return False
    msg = str(body.get("msg") or body.get("message") or "")
    if not msg:
        return False
    return maybe_mark_account_loi(
        account_id,
        msg,
        source=source,
        session=session,
    )
