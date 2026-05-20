# -*- coding: utf-8 -*-
"""Log phiên mini-game — format thống nhất (WS / đặt cược / kết quả)."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any

from xoso66_bet_assign import BetSlot

_round_console_lock = threading.RLock()


def _ts_hms() -> str:
    return time.strftime("%H:%M:%S")


def assign_bet_console_enabled() -> bool:
    """Auto-bet gán acc + HTTP: dùng header issue, khóa log phiên."""
    try:
        from xoso66_config_util import load_config

        ab = load_config().get("auto_bet")
        return (
            isinstance(ab, dict)
            and bool(ab.get("enabled"))
            and bool(ab.get("assign_bets_enabled"))
        )
    except Exception:
        return False


@contextmanager
def round_console_lock():
    """Tránh 2 phiên in xen kẽ (mỗi phiên = 1 khối log)."""
    with _round_console_lock:
        yield


def log_round_bet_header(
    *,
    issue: str,
    game_label: str,
    jackpot_vnd: float = 0,
) -> None:
    """Giữ cho code cũ; auto-bet dùng log_round_start_line (cùng format WS)."""
    log_round_start_line(
        game_label=game_label,
        jackpot_vnd=jackpot_vnd,
        issue=issue,
    )


def log_round_start_line(
    *,
    game_label: str,
    jackpot_vnd: float = 0,
    issue: str = "",
) -> None:
    """Cùng dòng WS: BẮT ĐẦU PHIÊN — gọi sau round_start_log_delay_sec."""
    jp = _fmt_vnd(jackpot_vnd) if jackpot_vnd else "—"
    iss = f" | issue={issue}" if issue else ""
    with _round_console_lock:
        print(
            f"\n[{_ts_hms()}] BẮT ĐẦU PHIÊN - {game_label}{iss} - Jackpot : {jp}",
            flush=True,
        )


def log_round_bet_footer(
    *,
    issue: str,
    ok_n: int,
    total: int,
    fail_n: int = 0,
) -> None:
    """Giữ API; không in console."""
    return


def log_round_result_header(*, issue: str) -> None:
    with _round_console_lock:
        print(f"  [{issue}] Kết quả:", flush=True)


def _fmt_vnd(n: int | float) -> str:
    try:
        return f"{int(round(float(n))):,}"
    except (TypeError, ValueError):
        return str(n)


def _side_abbr(side: str) -> str:
    s = str(side or "").lower()
    return "T" if s in ("tai", "tài", "big") else "X"


def log_ws_connecting(username: str) -> None:
    """Giống LC79 — báo ngay khi token OK, trước khi WS connect."""
    user = str(username or "?").strip()
    print(f"🔐 [{user}] token OK, kết nối WS", flush=True)


def log_ws_connected(username: str, *, account_id: str = "") -> None:
    user = str(username or account_id or "?").strip()
    print(f"✅ [{user}] WS đã kết nối", flush=True)


def log_game_bet_start(
    game_label: str,
    *,
    jackpot_vnd: float = 0,
    issue: str = "",
    game_id: int | None = None,
) -> None:
    jp = f" (Hũ {_fmt_vnd(jackpot_vnd)})" if jackpot_vnd else ""
    extra = f" issue={issue}" if issue else ""
    gid = f" [game_id={game_id}]" if game_id is not None else ""
    print(f"- Game {game_label}{gid} bắt đầu đặt cược{jp}{extra}", flush=True)


def log_bet_user(username: str, side: str, amount_vnd: int) -> None:
    print(f"  -- {username} {_side_abbr(side)} {_fmt_vnd(amount_vnd)}", flush=True)


def log_bet_plan(
    slots: list[BetSlot],
    *,
    game_label: str,
    jackpot_vnd: float = 0,
    issue: str = "",
    game_id: int | None = None,
) -> None:
    log_game_bet_start(
        game_label,
        jackpot_vnd=jackpot_vnd,
        issue=issue,
        game_id=game_id,
    )
    for s in slots:
        log_bet_user(s.username, s.side, s.amount_vnd)
    tai = sum(x.amount_vnd for x in slots if x.side == "tai")
    xiu = sum(x.amount_vnd for x in slots if x.side == "xiu")
    print(
        f"  (Tài {_fmt_vnd(tai)} | Xỉu {_fmt_vnd(xiu)} | {len(slots)} acc)",
        flush=True,
    )


def normalize_winning_side(open_data: dict[str, Any]) -> str | None:
    """open_info data → 'tai' | 'xiu' | None."""
    res = open_data.get("open_result") if isinstance(open_data.get("open_result"), dict) else {}
    raw = str(res.get("name") or res.get("result") or res.get("side") or "").strip().lower()
    if not raw:
        return None
    if raw in ("tai", "tài", "big", "t", "1"):
        return "tai"
    if raw in ("xiu", "xỉu", "small", "x", "0"):
        return "xiu"
    if "tài" in raw or raw.startswith("tai"):
        return "tai"
    if "xỉu" in raw or "xiu" in raw:
        return "xiu"
    return None


def winning_side_label(side: str) -> str:
    return "Tài" if side == "tai" else "Xỉu"


def log_round_result(
    game_label: str,
    winning: str,
    *,
    issue: str = "",
    numbers: str = "",
) -> None:
    w = winning_side_label(winning)
    extra = f" issue={issue}" if issue else ""
    nums = f" ({numbers})" if numbers else ""
    print(f"- Kết quả {game_label}: {w}{nums}{extra}", flush=True)


def log_user_settlement(
    username: str,
    *,
    won: bool,
    bet_vnd: int,
    payout_vnd: int = 0,
    balance_vnd: float | None = None,
    from_api: bool = False,
) -> None:
    if won:
        src = "API" if from_api else "lãi ×0.98 (hoàn ×1.98)"
        print(
            f"  -- {username} thắng +{_fmt_vnd(payout_vnd)} "
            f"(cược {_fmt_vnd(bet_vnd)} {src})",
            flush=True,
        )
    else:
        print(f"  -- {username} thua -{_fmt_vnd(bet_vnd)}", flush=True)
    if balance_vnd is not None:
        print(f"      balance DB: {_fmt_vnd(balance_vnd)}", flush=True)
