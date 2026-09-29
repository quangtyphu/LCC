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


def pending_bet_blocks_balance_increase(account_id: str, new_balance: float) -> bool:
    """
    Nick ∈ C (chờ KQ): chặn ghi số dư CAO hơn DB.

    Tránh ensure_session/persist_session/getBalance/WS stale đè số dư
    vừa trừ sau placeOrder (vd. 16,981 → lại 106,981 → gán cược vượt).

    Không áp dụng cho sync từ placeOrder (force=True): DB có thể đang thấp
    hơn thật (stale) nên postBalance placeOrder cao hơn DB vẫn phải ghi.
    """
    aid = str(account_id or "").strip()
    if not aid:
        return False
    try:
        from xoso66_auto_bet import pending_bet_account_ids

        if aid not in pending_bet_account_ids():
            return False
    except Exception:
        return False
    from xoso66_accounts_db import get_account

    row = get_account(aid) or {}
    try:
        db_bal = float(row.get("balance") or 0)
    except (TypeError, ValueError):
        db_bal = 0.0
    try:
        new_bal = float(new_balance)
    except (TypeError, ValueError):
        return False
    return new_bal > db_bal + 0.5


def sync_ws_balance_to_db(
    account_id: str, balance: float, *, force: bool = False
) -> None:
    """Ghi balance CMS + session_json.user_info.money (nếu có).

    force=True: tin nguồn (placeOrder) — bỏ chặn tăng số dư khi đang chờ KQ.
    """
    from xoso66_accounts_db import get_account, update_account

    aid = str(account_id).strip()
    if not aid:
        return
    try:
        bal_f = float(balance)
    except (TypeError, ValueError):
        return
    if not force and pending_bet_blocks_balance_increase(aid, bal_f):
        return
    patch: dict[str, Any] = {"balance": bal_f}
    row = get_account(aid) or {}
    sess = row.get("session_json")
    if isinstance(sess, dict):
        merged = dict(sess)
        ui = merged.get("user_info")
        if isinstance(ui, dict):
            ui2 = dict(ui)
            ui2["money"] = bal_f
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
    """Fallback in KQ nếu WS watch chưa in lúc claim open_info."""
    del win_total_return, slots, win_rate  # không dùng ước tính balance / acc

    winning = resolve_winning_side(open_data)
    if not winning:
        return
    dices = open_data_to_dices(open_data)
    iss = str(issue or open_data.get("issue") or "").strip()
    try:
        gid = int(open_data.get("game_id") or open_data.get("id") or 0)
    except (TypeError, ValueError):
        gid = 0
    if gid > 0:
        from xoso66_minigame_ws import note_round_result_logged

        if not note_round_result_logged(gid, iss):
            return

    from xoso66_round_log import log_round_result_header, round_console_lock

    with round_console_lock():
        log_round_result_header(
            issue=iss,
            winning_side=winning,
            dices=dices,
        )
