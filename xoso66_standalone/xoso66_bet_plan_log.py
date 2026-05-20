# -*- coding: utf-8 -*-
"""In kế hoạch cược phiên (format LC79) — chỉ log, không HTTP."""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass
from typing import Any

from xoso66_accounts_db import get_account
from xoso66_bet_assign import BetSlot


@dataclass
class PlannedBet:
    slot: BetSlot
    balance_before: int
    balance_after: int
    delay_sec: int


def _door_upper(side: str) -> str:
    return "TAI" if str(side).lower() in ("tai", "tài", "big") else "XIU"


def _balance_int(account_id: str) -> int:
    row = get_account(account_id) or {}
    try:
        return int(round(float(row.get("balance") or 0)))
    except (TypeError, ValueError):
        return 0


def _plan_delays(n: int, acfg: dict) -> list[int]:
    mode = str(acfg.get("bet_stagger_mode") or "random").strip().lower()
    if mode in ("sequential", "seq", "1s"):
        return list(range(1, max(1, n) + 1))
    dmin = int(acfg.get("bet_plan_delay_min_sec") or acfg.get("bet_delay_min_sec") or 5)
    dmax = int(acfg.get("bet_plan_delay_max_sec") or acfg.get("bet_delay_max_sec") or 25)
    if dmax < dmin:
        dmax = dmin
    return [random.randint(dmin, dmax) for _ in range(n)]


def build_planned_bets(slots: list[BetSlot], cfg: dict) -> list[PlannedBet]:
    acfg = cfg.get("auto_bet") if isinstance(cfg.get("auto_bet"), dict) else {}
    delays = _plan_delays(len(slots), acfg)
    planned: list[PlannedBet] = []
    for i, slot in enumerate(slots):
        bal = _balance_int(slot.account_id)
        amt = int(slot.amount_vnd)
        after = max(0, bal - amt)
        planned.append(
            PlannedBet(
                slot=slot,
                balance_before=bal,
                balance_after=after,
                delay_sec=delays[i] if i < len(delays) else 1,
            )
        )
    return planned


def log_lc79_bet_plan(planned: list[PlannedBet], *, show_stagger: bool = True) -> None:
    """➡️ từng user + 📊 tổng hai bên. show_stagger=False khi đặt HTTP thật (không in «Sau Xs»)."""
    from xoso66_round_log import round_console_lock

    with round_console_lock():
        for p in planned:
            s = p.slot
            door = _door_upper(s.side)
            tail = (
                f" Sau {str(p.delay_sec).rjust(3)}s"
                if show_stagger
                else ""
            )
            print(
                f"➡️  User {s.username.ljust(20)} "
                f"Balance={str(p.balance_before).rjust(8)} "
                f"→ Đặt {door.ljust(3)} {str(s.amount_vnd).rjust(7)} "
                f"(Còn lại {str(p.balance_after).rjust(8)})"
                f"{tail}",
                flush=True,
            )
        total_tai = sum(p.slot.amount_vnd for p in planned if p.slot.side == "tai")
        total_xiu = sum(p.slot.amount_vnd for p in planned if p.slot.side == "xiu")
        print(f"📊 Tổng Tài = {total_tai} | Tổng Xỉu = {total_xiu}", flush=True)


def _print_simulated_ok(p: PlannedBet) -> None:
    from xoso66_shutdown import stopping
    from xoso66_ws_balance import log_dice_bet

    if stopping():
        return
    s = p.slot
    log_dice_bet(
        s.username,
        side=s.side,
        amount_vnd=s.amount_vnd,
        balance=p.balance_after,
    )


def schedule_simulated_place_logs(
    planned: list[PlannedBet],
    *,
    base_delay_sec: float = 0,
) -> None:
    """In ✅ theo delay từng user (mô phỏng, không HTTP)."""
    for p in planned:
        wait = max(0.0, base_delay_sec + float(p.delay_sec))
        threading.Timer(wait, _print_simulated_ok, args=(p,)).start()


def log_and_maybe_simulate_place(
    slots: list[BetSlot],
    cfg: dict,
    *,
    simulate: bool | None = None,
) -> None:
    acfg = cfg.get("auto_bet") if isinstance(cfg.get("auto_bet"), dict) else {}
    if simulate is None:
        simulate = (not bool(acfg.get("place_orders", False))) and bool(
            acfg.get("log_simulated_place", True)
        )
    planned = build_planned_bets(slots, cfg)
    if not planned:
        return
    show_stagger = bool(simulate) or not bool(acfg.get("place_orders", False))
    log_lc79_bet_plan(planned, show_stagger=show_stagger)
    if simulate and acfg.get("log_simulated_place", True):
        schedule_simulated_place_logs(planned)
