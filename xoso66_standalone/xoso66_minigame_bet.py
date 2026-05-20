# -*- coding: utf-8 -*-
"""
Đặt cược mini-game — dùng chung mọi game (catalog xoso66_minigame_catalog).

WS (test/worker) đọc tín hiệu → gọi place_bet / place_bet_random.
Sau HTTP cược: BetTracker + log_ws_bet_message để xem WS báo cược & trả thưởng.
(Cập nhật balance site chính — làm sau.)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from xoso66_minigame_catalog import game_by_key, play_id_for_side
from xoso66_minigame_http import PLACE_ORDER_PATH, minigame_request


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def fmt_money(val: Any) -> str:
    if val is None:
        return "—"
    try:
        return f"{float(str(val).replace(',', '')):,.0f}"
    except ValueError:
        return str(val)


def parse_balance(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(str(val).replace(",", "").strip())
    except ValueError:
        return None


@dataclass
class BetRequest:
    game_key: str
    side: str  # tai | xiu | … (theo catalog plays)
    amount: int
    issue: str = ""  # tùy chọn; server thường tự gán phiên đang mở


@dataclass
class BetResult:
    ok: bool
    game_key: str
    game_id: int
    side: str
    play_id: int
    amount: int
    http_status: int
    code: Any
    msg: str
    data: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def issue(self) -> str:
        direct = str(self.data.get("issue") or "")
        if direct:
            return direct
        orders = self.data.get("orders")
        if isinstance(orders, dict):
            lst = orders.get("list")
            if isinstance(lst, list) and lst and isinstance(lst[0], dict):
                return str(lst[0].get("issue") or "")
        return ""

    @property
    def serial_no(self) -> str:
        direct = str(self.data.get("serial_no") or self.data.get("order_no") or "")
        if direct:
            return direct
        orders = self.data.get("orders")
        if isinstance(orders, dict):
            lst = orders.get("list")
            if isinstance(lst, list) and lst and isinstance(lst[0], dict):
                return str(lst[0].get("serial_no") or "")
        return ""

    @property
    def balance(self) -> float | None:
        """Số dư sau cược — từ ``data.balance`` (placeOrder response)."""
        return parse_balance(self.data.get("balance"))


def _gamename(game_key: str) -> str:
    return str(game_by_key(game_key).get("gamename") or "dice_md5")


def place_bet(
    session: dict,
    req: BetRequest,
    *,
    account_id: str = "",
    check_token: bool = True,
    http_timeout: int = 45,
) -> BetResult:
    """POST placeOrder — một cửa."""
    g = game_by_key(req.game_key)
    aid = (account_id or str(session.get("id") or session.get("account_id") or "")).strip()
    if check_token and aid:
        from xoso66_minigame_refresh import ensure_user_token_for_bet

        ready, msg = ensure_user_token_for_bet(
            session, aid, game_key=req.game_key, allow_slow_refresh=False
        )
        if not ready:
            return BetResult(
                ok=False,
                game_key=req.game_key,
                game_id=int(g["game_id"]),
                side=req.side,
                play_id=0,
                amount=int(req.amount),
                http_status=0,
                code=0,
                msg=msg,
            )
    game_id = int(g["game_id"])
    play_id = play_id_for_side(req.game_key, req.side)
    order: dict[str, Any] = {
        "play_id": play_id,
        "price": int(req.amount),
        "content": "",
    }
    body: dict[str, Any] = {"game_id": game_id, "orders": [order]}
    if req.issue:
        body["issue"] = str(req.issue)

    status, js = minigame_request(
        session,
        "POST",
        PLACE_ORDER_PATH,
        game_id=game_id,
        gamename=_gamename(req.game_key),
        json_body=body,
        timeout=http_timeout,
    )
    ok = isinstance(js, dict) and js.get("code") == 1
    data = js.get("data") if isinstance(js, dict) and isinstance(js.get("data"), dict) else {}
    if ok and aid:
        try:
            from xoso66_accounts_db import record_daily_bet

            # Chỉ cộng tổng cược ngày sau placeOrder thành công (code=1).
            record_daily_bet(aid, int(req.amount))
        except Exception:
            pass
        bal = parse_balance(data.get("balance"))
        if bal is not None:
            try:
                from xoso66_ws_balance import sync_ws_balance_to_db

                sync_ws_balance_to_db(aid, bal)
            except Exception:
                pass
    return BetResult(
        ok=ok,
        game_key=req.game_key,
        game_id=game_id,
        side=req.side,
        play_id=play_id,
        amount=int(req.amount),
        http_status=status,
        code=js.get("code") if isinstance(js, dict) else None,
        msg=str(js.get("msg") or "") if isinstance(js, dict) else "",
        data=data,
        raw=js if isinstance(js, dict) else {},
    )


def place_bet_random(
    session: dict,
    *,
    game_key: str,
    amount: int,
    sides: tuple[str, ...] = ("tai", "xiu"),
    issue: str = "",
) -> BetResult:
    import random

    side = random.choice(sides)
    return place_bet(session, BetRequest(game_key=game_key, side=side, amount=amount, issue=issue))


def log_http_bet(result: BetResult, *, signal_issue: str = "") -> None:
    """In kết quả HTTP ngay sau khi gọi placeOrder."""
    tag = "OK" if result.ok else "FAIL"
    sig = f"  (tín hiệu WS issue={signal_issue})" if signal_issue else ""
    print(
        f"[{_ts()}] HTTP CƯỢC {tag}{sig}  "
        f"{result.game_key} game_id={result.game_id}  "
        f"{result.side.upper()} play_id={result.play_id}  price={result.amount}",
        flush=True,
    )
    if result.issue or result.serial_no or result.balance is not None:
        print(
            f"         issue={result.issue or '—'}  serial={result.serial_no or '—'}  "
            f"balance={fmt_money(result.balance)}",
            flush=True,
        )
    if not result.ok:
        print(f"         code={result.code}  msg={result.msg}", flush=True)
    elif result.data:
        extra = {k: v for k, v in result.data.items() if k not in ("issue", "serial_no", "balance")}
        if extra:
            print(
                f"         data={json.dumps(extra, ensure_ascii=False)[:400]}",
                flush=True,
            )


@dataclass
class TrackedBet:
    signal_issue: str
    game_key: str
    game_id: int
    side: str
    amount: int
    placed_at: float
    bet_ok: bool = False
    http_issue: str = ""
    serial_no: str = ""
    http_balance: Any = None
    balance_after_bet: float | None = None


class BetTracker:
    """Theo dõi lệnh vừa cược để khớp message WS (kết quả / bet_data)."""

    def __init__(self) -> None:
        self.by_http_issue: dict[str, TrackedBet] = {}
        self.by_signal_issue: dict[str, TrackedBet] = {}
        self._last: TrackedBet | None = None

    def register_http(self, result: BetResult, *, signal_issue: str = "") -> TrackedBet:
        tb = TrackedBet(
            signal_issue=signal_issue,
            game_key=result.game_key,
            game_id=result.game_id,
            side=result.side,
            amount=result.amount,
            placed_at=time.time(),
            bet_ok=result.ok,
            http_issue=result.issue,
            serial_no=result.serial_no,
            http_balance=result.balance,
            balance_after_bet=parse_balance(result.balance),
        )
        self._last = tb
        if signal_issue:
            self.by_signal_issue[signal_issue] = tb
        if result.issue:
            self.by_http_issue[result.issue] = tb
        return tb

    def lookup_issue(self, issue: str) -> TrackedBet | None:
        if issue in self.by_http_issue:
            return self.by_http_issue[issue]
        if issue in self.by_signal_issue:
            return self.by_signal_issue[issue]
        return None


def _watch_gid(data: dict[str, Any]) -> int:
    return int(data.get("game_id") or data.get("id") or 0)


def _open_side(res: dict[str, Any]) -> str:
    code = str(res.get("code") or res.get("play_code") or "").lower()
    if code in ("big", "tai"):
        return "tai"
    if code in ("small", "xiu"):
        return "xiu"
    name = str(res.get("name") or res.get("result") or "").lower()
    if "tài" in name or name == "tai":
        return "tai"
    if "xỉu" in name or "xiu" in name:
        return "xiu"
    return ""


class CompactRoundLogger:
    """Mỗi phiên tối đa 5 dòng: bắt đầu → cược → kết quả → thắng/thua → jackpot."""

    def __init__(self, *, game_id: int) -> None:
        self.game_id = game_id
        self._started: set[str] = set()
        self._open_logged: set[str] = set()
        self._pending_jackpot: Any = None
        self._pending_settle: dict[str, Any] | None = None

    def _print(self, label: str, msg: str) -> None:
        print(f"[{_ts()}] {label}  {msg}", flush=True)

    def on_round_start(
        self,
        issue: str,
        *,
        begin_time: str = "",
        end_time: str = "",
        is_open: Any = None,
        countdown: Any = None,
        ws_late_sec: float | None = None,
        from_next_info: bool = False,
        bet_window_sec: float = 20.0,
    ) -> None:
        parts = [f"issue={issue}"]
        if begin_time and end_time:
            parts.append(f"{begin_time} → {end_time}")
        if from_next_info:
            cd = countdown if countdown is not None else "?"
            parts.append(f"open_info→next_info  cd={cd}")
        elif is_open is not None and countdown is not None:
            if int(is_open) == 1:
                parts.append(f"mở cược ~{countdown}s để đặt")
            else:
                parts.append(f"đóng cược ~{countdown}s (chờ KQ)")
        elif countdown is not None:
            parts.append(f"cd={countdown}s")
        if ws_late_sec is not None and ws_late_sec > 1:
            parts.append(f"WS trễ {ws_late_sec:.0f}s")
        self._started.add(issue)
        print(flush=True)
        self._print("BẮT ĐẦU PHIÊN", "  |  ".join(parts))

    def _flush_stale_settle(self) -> None:
        if not self._pending_settle:
            return
        old = str(self._pending_settle.get("issue") or "?")
        self._print("THẮNG/THUA", f"— issue={old} (chưa nhận WS balance)")
        self._pending_settle = None

    def _settle_from_ws_balance(self, new_bal: float) -> None:
        p = self._pending_settle
        if not p:
            return
        self._pending_settle = None
        baseline = p.get("baseline")
        if baseline is None:
            win_side = p.get("win_side") or ""
            side = p.get("side") or ""
            if win_side and side:
                guess = "THẮNG" if win_side == side else "THUA"
                self._print(
                    "THẮNG/THUA",
                    f"{guess} (ước tính KQ)  balance={fmt_money(new_bal)} (WS)",
                )
            else:
                self._print("THẮNG/THUA", f"balance={fmt_money(new_bal)} (WS)")
            return
        delta = new_bal - float(baseline)
        if delta > 0.5:
            self._print(
                "THẮNG/THUA",
                f"THẮNG  +{delta:,.0f}  {fmt_money(baseline)} → {fmt_money(new_bal)} (WS)",
            )
        else:
            self._print(
                "THẮNG/THUA",
                f"THUA  balance={fmt_money(new_bal)} (WS, không đổi sau cược)",
            )

    def on_open_result(self, data: dict[str, Any], tracker: BetTracker) -> None:
        """Kết quả phiên vừa xong (open_info.data)."""
        issue = str(data.get("issue") or "")
        if not issue or issue in self._open_logged:
            return
        self._flush_stale_settle()
        self._open_logged.add(issue)

        res = data.get("open_result") or {}
        nums = data.get("open_numbers") or res.get("open_numbers") or "?"
        side = res.get("name") or res.get("result") or "?"
        self._print(
            "KẾT QUẢ",
            f"issue={issue}  {nums}  → {side} (tổng={res.get('sum')})",
        )

        tb = tracker.lookup_issue(issue)
        if tb and not tb.bet_ok:
            self._print("THẮNG/THUA", "— (không vào kèo)")
        elif tb and tb.bet_ok:
            self._pending_settle = {
                "issue": issue,
                "baseline": tb.balance_after_bet,
                "amount": tb.amount,
                "side": tb.side,
                "win_side": _open_side(res),
            }
        else:
            self._print("THẮNG/THUA", "—")

        jp = self._pending_jackpot
        if jp is None and res.get("is_jackpot"):
            jp = res.get("jackpot") or res.get("money")
        self._print("JACKPOT", fmt_money(jp) if jp is not None else "—")
        self._pending_jackpot = None

    def on_bet_skip(self, issue: str, reason: str) -> None:
        self._print("ĐẶT CƯỢC", f"BỎ QUA issue={issue} — {reason}")

    def on_bet(self, result: BetResult, *, signal_issue: str = "") -> None:
        if result.ok:
            self._print(
                "ĐẶT CƯỢC",
                f"{result.side.upper()} {result.amount:,}  OK  "
                f"issue={result.issue or signal_issue}  balance={fmt_money(result.balance)}",
            )
        else:
            self._print(
                "ĐẶT CƯỢC",
                f"{result.side.upper()} {result.amount:,}  FAIL  "
                f"code={result.code}  {result.msg}",
            )

    def on_ws(self, obj: Any, tracker: BetTracker) -> bool:
        if not isinstance(obj, dict):
            return False
        t = str(obj.get("type") or "").lower()
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        gid = _watch_gid(data) if data else _watch_gid(obj)
        if gid and gid != self.game_id:
            return False

        if t in ("balance", "g_balance"):
            bal = parse_balance(data.get("balance"))
            if bal is not None and self._pending_settle:
                self._settle_from_ws_balance(bal)
            return True

        if t == "jackpot_money":
            money = data.get("money") if data.get("money") is not None else data.get("jackpot")
            if money is not None:
                self._pending_jackpot = money
            return True

        return False


def log_ws_bet_message(
    obj: Any,
    tracker: BetTracker,
    *,
    game_id: int | None = None,
) -> bool:
    """
    In WS liên quan cược / trả thưởng. Trả True nếu đã log.
    """
    if not isinstance(obj, dict):
        return False
    t = str(obj.get("type") or "").lower()
    data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
    gid = _watch_gid(data) if data else _watch_gid(obj)
    if game_id is not None and gid and gid != game_id:
        return False

    if t == "bet_data":
        print(
            f"[{_ts()}] WS CƯỢC  bet_data  game_id={gid}  "
            f"{json.dumps(data, ensure_ascii=False)[:600]}",
            flush=True,
        )
        return True

    if t in ("g_open_info", "open_info"):
        issue = str(data.get("issue") or "")
        res = data.get("open_result") or {}
        win_side = _open_side(res)
        tb = tracker.lookup_issue(issue) if issue else tracker._last
        ours = ""
        if tb:
            won = win_side and win_side == tb.side
            ours = (
                f"  | lệnh ta: {tb.side.upper()} {tb.amount} "
                f"signal={tb.signal_issue} http_issue={tb.http_issue}"
            )
            if win_side:
                ours += f"  → {'THẮNG' if won else 'THUA'}"
        jp = " | HŨ" if res.get("is_jackpot") else ""
        print(
            f"[{_ts()}] WS TRẢ THƯỞNG  issue={issue}  game_id={gid}  "
            f"{data.get('open_numbers')}  → {res.get('name') or res.get('result')} "
            f"(sum={res.get('sum')}){jp}{ours}",
            flush=True,
        )
        return True

    if any(x in t for x in ("order", "win", "settle", "prize", "payout", "reward")):
        print(
            f"[{_ts()}] WS $  {t}  {json.dumps(data or obj, ensure_ascii=False)[:500]}",
            flush=True,
        )
        return True

    for src in (data, obj):
        if not isinstance(src, dict):
            continue
        for k, v in src.items():
            if str(k).lower() in ("money", "balance", "win_money", "prize") and v is not None:
                print(f"[{_ts()}] WS $  {t}  {k}={fmt_money(v)}", flush=True)
                return True
    return False
