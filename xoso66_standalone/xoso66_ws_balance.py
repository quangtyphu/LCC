# -*- coding: utf-8 -*-
"""
Balance mini-game → DB chỉ từ:
  - placeOrder HTTP (sau đặt cược), hoặc
  - WS {"type":"balance", ...} (sau thắng / server push).

Log 🎲: chỉ KQ phiên (Cược/KQ/Dices/Prize) — không ghi DB, không phải số dư thật.
Số dư thật sau thắng: WS balance → sync DB + dòng ✅ giống sau đặt cược.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
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


@dataclass
class _PendingSettle:
    username: str
    issue: str


_pending_settle_lock = threading.Lock()
_pending_settle: dict[str, _PendingSettle] = {}


def _result_balance_wait_sec() -> float:
    try:
        from xoso66_config_util import load_config

        ab = load_config().get("auto_bet")
        if isinstance(ab, dict):
            return float(ab.get("result_balance_wait_sec") or 30)
    except Exception:
        pass
    return 30.0


def _register_settle_wait(account_id: str, username: str, issue: str) -> None:
    aid = str(account_id).strip()
    if not aid:
        return
    iss = str(issue or "").strip()
    with _pending_settle_lock:
        _pending_settle[aid] = _PendingSettle(
            username=str(username or aid),
            issue=iss,
        )

    wait = _result_balance_wait_sec()

    def _timeout() -> None:
        time.sleep(max(1.0, wait))
        with _pending_settle_lock:
            cur = _pending_settle.get(aid)
            if cur is not None and cur.issue == iss:
                _pending_settle.pop(aid, None)

    threading.Thread(
        target=_timeout, name=f"ws-bal-wait-{aid[:8]}", daemon=True
    ).start()


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


def log_dice_result(
    username: str,
    *,
    won: bool,
    bet_side: str,
    result_side: str,
    dices: list[int],
    prize: int,
    issue: str = "",
) -> None:
    """🎲 chỉ thông báo KQ — không dùng cho DB / gán cược."""
    if not won:
        return
    d = dices if dices else [0, 0, 0]
    bs = winning_side_label(normalize_bet_side(bet_side))
    rs = winning_side_label(result_side)
    from xoso66_round_log import round_console_lock

    with round_console_lock():
        print(
            f"🎲 [{username}] Thắng phiên | Cược={bs} KQ={rs} | "
            f"Dices={d} | Prize={int(prize)}",
            flush=True,
        )


def log_dice_balance_ws(
    username: str,
    balance: int | float,
    *,
    issue: str = "",
) -> None:
    """Số dư sau thắng từ WS — format giống dòng ✅ sau đặt cược."""
    try:
        bal_i = int(round(float(balance)))
    except (TypeError, ValueError):
        bal_i = 0
    user = str(username or "").strip()
    from xoso66_round_log import round_console_lock

    line = (
        f"✅ [{user.ljust(15)}] "
        f"Thắng phiên          "
        f"| Số dư mới = {str(bal_i).rjust(10)}"
    )
    with round_console_lock():
        print(line, flush=True)


def on_ws_balance_message(account_id: str, balance: float) -> bool:
    """WS type balance — sync DB; nếu đang chờ KQ thắng thì in ✅ Số dư mới."""
    aid = str(account_id).strip()
    if not aid:
        return False
    sync_ws_balance_to_db(aid, balance)
    pending: _PendingSettle | None = None
    with _pending_settle_lock:
        pending = _pending_settle.pop(aid, None)
    if pending is not None:
        log_dice_balance_ws(pending.username, balance, issue=pending.issue)
    return True


def log_round_settlements(
    slots: list[Any],
    open_data: dict[str, Any],
    *,
    win_rate: float = 0.98,
    win_total_return: float | None = None,
    issue: str = "",
) -> None:
    """In 🎲 thắng (chỉ hiển thị); chờ WS balance để ghi DB + in ✅."""
    del win_total_return  # không dùng ước tính balance

    winning = resolve_winning_side(open_data)
    if not winning:
        return
    dices = open_data_to_dices(open_data)
    iss = str(issue or open_data.get("issue") or "").strip()
    profit_rate = win_profit_rate(win_rate)

    from xoso66_round_log import log_round_result_header, round_console_lock

    with round_console_lock():
        log_round_result_header(issue=iss)
        for slot in slots:
            bet_side = normalize_bet_side(slot.side)
            won = bet_side == winning
            bet = int(slot.amount_vnd)
            prize = int(round(bet * profit_rate)) if won else 0
            if won:
                _register_settle_wait(
                    str(slot.account_id),
                    str(slot.username or slot.account_id),
                    iss,
                )
            log_dice_result(
                slot.username,
                won=won,
                bet_side=bet_side,
                result_side=winning,
                dices=dices,
                prize=prize,
                issue=iss,
            )
