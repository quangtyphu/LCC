# -*- coding: utf-8 -*-
"""
Nổ hũ: Telegram chỉ khi phiên đó auto_bet đã đặt cược (có slot trong _pending_bets).

Điều kiện: open_result.is_jackpot + có lệnh ta trên issue đó.
"""

from __future__ import annotations

import threading
from typing import Any

_last_pool_lock = threading.Lock()
_last_pool_money: dict[int, float] = {}
_notified_issues: set[str] = set()
_notified_lock = threading.Lock()


def _parse_money(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(str(val).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _fmt_vnd(val: float | None) -> str:
    if val is None:
        return "N/A"
    if val >= 1_000_000:
        return f"{val:,.0f} VND"
    return f"{val:,.2f} VND".rstrip("0").rstrip(".") + " VND"


def record_jackpot_pool(game_id: int, money: Any) -> None:
    """Cập nhật từ WS type=jackpot_money (quỹ đang chạy)."""
    amount = _parse_money(money)
    if amount is None or int(game_id) <= 0:
        return
    with _last_pool_lock:
        _last_pool_money[int(game_id)] = amount


def last_pool_money(game_id: int) -> float | None:
    with _last_pool_lock:
        return _last_pool_money.get(int(game_id))


def is_jackpot_open_data(open_data: dict[str, Any]) -> bool:
    res = open_data.get("open_result") if isinstance(open_data.get("open_result"), dict) else {}
    return bool(res.get("is_jackpot"))


def extract_jackpot_amount(open_data: dict[str, Any], game_id: int) -> float | None:
    res = open_data.get("open_result") if isinstance(open_data.get("open_result"), dict) else {}
    for src in (res, open_data):
        if not isinstance(src, dict):
            continue
        for key in (
            "jackpot",
            "jackpot_money",
            "money",
            "jackpot_amount",
            "prize",
            "win_money",
            "amount",
        ):
            v = _parse_money(src.get(key))
            if v is not None and v > 0:
                return v
    pool = last_pool_money(game_id)
    if pool is not None and pool > 0:
        return pool
    try:
        from xoso66_minigame_jackpot_store import get_jackpot_store

        return get_jackpot_store().get_money(int(game_id))
    except Exception:
        return None


def _issue_notify_key(game_id: int, issue: str) -> str:
    return f"{int(game_id)}:{str(issue).strip()}"


def _claim_notify(game_id: int, issue: str) -> bool:
    key = _issue_notify_key(game_id, issue)
    with _notified_lock:
        if key in _notified_issues:
            return False
        _notified_issues.add(key)
        if len(_notified_issues) > 800:
            _notified_issues.clear()
        return True


def format_jackpot_hit_message(
    *,
    game_id: int,
    issue: str,
    open_data: dict[str, Any],
    jackpot_vnd: float | None,
    bet_slots: list[Any],
    game_label: str = "",
) -> str:
    res = open_data.get("open_result") if isinstance(open_data.get("open_result"), dict) else {}
    nums = str(open_data.get("open_numbers") or res.get("open_numbers") or "?")
    side = res.get("name") or res.get("result") or "?"
    lines = [
        f"Nổ hũ — {game_label or f'game_id={game_id}'}",
        f"Phiên: {issue}",
        f"Kết quả: {nums} ({side})",
        f"Số tiền nổ hũ: {_fmt_vnd(jackpot_vnd)}",
    ]
    if bet_slots:
        parts = []
        for s in bet_slots[:10]:
            u = getattr(s, "username", None) or getattr(s, "account_id", "?")
            sd = getattr(s, "side", "?")
            amt = int(getattr(s, "amount", 0) or 0)
            parts.append(f"{u} {str(sd).upper()} {amt:,}")
        lines.append("Lệnh ta: " + "; ".join(parts))
        if len(bet_slots) > 10:
            lines.append(f"(+{len(bet_slots) - 10} nick)")
    return "\n".join(lines)


def notify_jackpot_hit_for_our_bets(
    game_id: int,
    issue: str,
    open_data: dict[str, Any],
    bet_slots: list[Any],
    *,
    game_label: str = "",
    cfg: dict | None = None,
) -> bool:
    """Telegram chỉ khi ta có cược phiên này và is_jackpot."""
    issue = str(issue or "").strip()
    if not issue or not bet_slots or not is_jackpot_open_data(open_data):
        return False
    if not _claim_notify(game_id, issue):
        return False

    jp = extract_jackpot_amount(open_data, game_id)
    from xoso66_telegram_notify import notify_auto_bet

    msg = format_jackpot_hit_message(
        game_id=game_id,
        issue=issue,
        open_data=open_data,
        jackpot_vnd=jp,
        bet_slots=bet_slots,
        game_label=game_label,
    )
    ok = notify_auto_bet(msg, cfg=cfg, prefix="XOSO66")
    print(
        f"[JACKPOT-HIT] {game_label or game_id} issue={issue} "
        f"bets={len(bet_slots)} amount={_fmt_vnd(jp)} tele={'OK' if ok else 'FAIL'}",
        flush=True,
    )
    return ok
