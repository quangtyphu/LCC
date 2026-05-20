# -*- coding: utf-8 -*-
"""
Balance mini-game → DB chỉ từ:
  - placeOrder HTTP (sau đặt cược), hoặc
  - WS {"type":"balance", ...} (sau thắng / server push).

Log KQ phiên: một dòng issue + Tài/Xỉu + Dices (không in từng acc thắng).
WS balance → chỉ sync DB (không in console sau thắng).
"""

from __future__ import annotations

from typing import Any

from xoso66_round_log import normalize_winning_side, winning_side_label


def win_profit_rate(win_rate: float = 0.98) -> float:
    """Phần lãi trên tiền cược (vd. 0.98 = +98% lãi)."""
    return float(win_rate)


def parse_ws_balance(data: dict[str, Any]) -> float | None:
    raw = data.get("balance")
    if raw is None:
        raw = data.get("money")
    if raw is None:
        return None
    try:
        return float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def sync_ws_balance_to_db(account_id: str, balance: float) -> None:
    """Ghi balance CMS + session_json.user_info.money (nếu có)."""
    from xoso66_accounts_db import get_account, update_account

    aid = str(account_id).strip()
    if not aid:
        return
    patch: dict[str, Any] = {"balance": balance}
    row = get_account(aid) or {}
    sess = row.get("session_json")
    if isinstance(sess, dict):
        merged = dict(sess)
        ui = merged.get("user_info")
        if isinstance(ui, dict):
            ui2 = dict(ui)
            ui2["money"] = balance
            merged["user_info"] = ui2
            patch["session_json"] = merged
    try:
        update_account(aid, patch)
    except KeyError:
        pass


def normalize_bet_side(side: str) -> str:
    s = str(side or "").strip().lower()
    if s in ("tai", "tài", "big", "t", "1"):
        return "tai"
    return "xiu"


def open_data_to_dices(open_data: dict[str, Any]) -> list[int]:
    res = open_data.get("open_result") if isinstance(open_data.get("open_result"), dict) else {}
    raw = open_data.get("open_numbers") or res.get("open_numbers") or ""
    out: list[int] = []
    for part in str(raw).replace(";", ",").split(","):
        p = part.strip()
        if p.isdigit():
            out.append(int(p))
    return out


def resolve_winning_side(open_data: dict[str, Any]) -> str | None:
    """Ưu tiên open_result từ server; fallback tổng 3 xúc xắc."""
    winning = normalize_winning_side(open_data)
    dices = open_data_to_dices(open_data)
    if winning:
        return winning
    if len(dices) >= 3:
        return "tai" if sum(dices[:3]) >= 11 else "xiu"
    return None


def log_dice_bet(
    username: str,
    *,
    side: str,
    amount_vnd: int,
    balance: int | float,
    issue: str = "",
) -> None:
    """Format giống LC79 ws_events bet-result (balance từ response placeOrder)."""
    door = winning_side_label(normalize_bet_side(side))
    try:
        bal_i = int(round(float(balance)))
    except (TypeError, ValueError):
        bal_i = 0
    user = str(username or "").strip()
    from xoso66_round_log import round_console_lock

    line = (
        f"✅ [{user.ljust(15)}] "
        f"Đặt cược {door.ljust(4)} "
        f"- {str(int(amount_vnd)).rjust(8)} "
        f"| Số dư mới = {str(bal_i).rjust(10)}"
    )
    with round_console_lock():
        print(line, flush=True)


def on_ws_balance_message(account_id: str, balance: float) -> bool:
    """WS type balance — sync DB (không in console)."""
    aid = str(account_id).strip()
    if not aid:
        return False
    sync_ws_balance_to_db(aid, balance)
    return True


def log_round_settlements(
    slots: list[Any],
    open_data: dict[str, Any],
    *,
    win_rate: float = 0.98,
    win_total_return: float | None = None,
    issue: str = "",
) -> None:
    """In KQ phiên (một dòng); WS balance sau đó chỉ sync DB."""
    del win_total_return, slots, win_rate  # không dùng ước tính balance / acc

    winning = resolve_winning_side(open_data)
    if not winning:
        return
    dices = open_data_to_dices(open_data)
    iss = str(issue or open_data.get("issue") or "").strip()

    from xoso66_round_log import log_round_result_header, round_console_lock

    with round_console_lock():
        log_round_result_header(
            issue=iss,
            winning_side=winning,
            dices=dices,
        )
