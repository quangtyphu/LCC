# -*- coding: utf-8 -*-
"""
Lỗi site nghiêm trọng → đổi status acc sang «Lỗi» và dừng auto-mission.

«Thao tác … lặp lại quá thường xuyên» và HTTP 475 = tạm thời (CF/spam) —
xử lý bằng login backoff trong xoso66_session, KHÔNG đánh STATUS_LỖI.

«Mã xác nhận không chính xác» (captcha) cũng không fatal —
login/register tự giải Capsolver và retry.
"""

from __future__ import annotations

from typing import Any

# Lỗi login/phiên không tự hết — đánh Lỗi ngay (không chờ hết poll).
_FATAL_SESSION_MARKERS: tuple[str, ...] = (
    "login thất bại",
    "login that bai",
    "sessioninvaliderror",
    "http 475",
    "cloudflare chặn post /user/login",
    "đã đánh lỗi",
)

# Poll auto-mission: cooldown/backoff — thử lại; hết poll mà vẫn vậy → Lỗi.
_TRANSIENT_SESSION_MARKERS: tuple[str, ...] = (
    "login cooldown",
    "login backoff",
    "sau login + refresh cf vẫn không getbalance",
    "getbalance vẫn fail",
    "getbalance thất bại",
    "giải mã response",
    "incorrect padding",
    "decrypt",
)

# Chỉ lỗi thật sự cần gỡ acc khỏi pool. Rate-limit tạm không nằm đây.
_FATAL_MSG_MARKERS: tuple[str, ...] = ()


def is_fatal_system_error_msg(msg: str) -> bool:
    m = str(msg or "").strip().lower()
    if not m:
        return False
    if any(marker in m for marker in _FATAL_SESSION_MARKERS):
        return True
    if not _FATAL_MSG_MARKERS:
        return False
    return any(marker in m for marker in _FATAL_MSG_MARKERS)


def is_transient_session_error(msg: str) -> bool:
    m = str(msg or "").strip().lower()
    if not m:
        return False
    return any(marker in m for marker in _TRANSIENT_SESSION_MARKERS)


def is_session_recovery_error(msg: str) -> bool:
    """Phiên/getBalance/login — auto-mission poll; hết poll mà vẫn lỗi → Lỗi."""
    m = str(msg or "").strip().lower()
    if not m:
        return False
    if is_fatal_system_error_msg(m) or is_transient_session_error(m):
        return True
    needles = (
        "thông tin phiên không hợp lệ",
        "thong tin phien khong hop le",
        "phiên không hợp lệ",
        "phien khong hop le",
        "session/login",
        "chưa đăng nhập",
        "chua dang nhap",
    )
    return any(x in m for x in needles)


def account_loi_for_unresolved_session(
    account_id: str,
    msg: str,
    *,
    source: str = "",
) -> bool:
    """
    Hết poll / không cứu được phiên → status «Lỗi», xóa hàng đợi auto-mission.
    """
    if not is_session_recovery_error(msg):
        return False
    from xoso66_accounts_db import (
        STATUS_LOI,
        get_account,
        set_account_status,
        username_for_log,
    )

    aid = _resolve_account_id(account_id)
    if not aid:
        return False
    row = get_account(aid) or {}
    u = username_for_log(aid, row)
    short_msg = str(msg).strip()[:320]
    reason = f"{source}: {short_msg}" if source else short_msg
    if str(row.get("status") or "").strip() != STATUS_LOI:
        set_account_status(aid, STATUS_LOI, reason=reason)
    else:
        print(
            f"[ACCOUNT] {u}: phiên/login (đã Lỗi) — {short_msg}",
            flush=True,
        )
    _cancel_mission_queue(aid)
    print(
        f"[AUTO-MISSION] {u}: hết poll / không login được — → Lỗi ({short_msg})",
        flush=True,
    )
    return True


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
    short_msg = str(msg).strip()[:320]
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
