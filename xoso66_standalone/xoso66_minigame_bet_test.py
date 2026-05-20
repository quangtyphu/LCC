# -*- coding: utf-8 -*-
"""
Test flow: open_info (~30s) → BẮT ĐẦU PHIÊN → delay 12–20s → HTTP cược.

  python xoso66_minigame_bet_test.py
  python xoso66_minigame_bet_test.py -u tenuser --max-bets 2

Không truyền -u/-a → nhập Username (giống xoso66_minigame_ws.py).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import random
import sys
from datetime import datetime
from typing import Any

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

GAME_KEY = "taixiu_dai_loc"
GAME_ID = 9
BET_DELAY_MIN = 12.0
BET_DELAY_MAX = 20.0

from xoso66_minigame_bet import BetTracker, CompactRoundLogger, place_bet_random
from xoso66_minigame_http import ws_url_from_token
from xoso66_minigame_ws import (
    _connect_ws,
    _decode_message,
    _is_ping_payload,
    _pong_reply,
    _ts,
    _watch_gid,
    resolve_account_id,
    ws_ping_loop,
    ws_send_subscribes,
)


class RoundState:
    def __init__(self) -> None:
        self.last_issue = ""
        self.bet_issues: set[str] = set()
        self.pending: set[str] = set()

    def accept_new_round(self, issue: str) -> bool:
        if not issue or issue in self.bet_issues or issue in self.pending:
            return False
        self.pending.add(issue)
        return True

    def done(self, issue: str) -> None:
        self.pending.discard(issue)
        self.bet_issues.add(issue)

    def skip(self, issue: str) -> None:
        self.pending.discard(issue)
        self.bet_issues.add(issue)


def _parse_dt(time_str: str) -> datetime | None:
    if not time_str:
        return None
    try:
        return datetime.strptime(str(time_str).strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _seconds_until(time_str: str) -> float | None:
    end = _parse_dt(time_str)
    if end is None:
        return None
    return (end - datetime.now()).total_seconds()


async def _bet_after_delay(
    session: dict,
    account_id: str,
    signal_issue: str,
    *,
    amount: int,
    delay_sec: float,
    rounds: RoundState,
    tracker: BetTracker,
    rlog: CompactRoundLogger,
) -> None:
    try:
        await asyncio.sleep(delay_sec)

        result = await asyncio.to_thread(
            place_bet_random,
            session,
            game_key=GAME_KEY,
            amount=amount,
        )
        rlog.on_bet(result, signal_issue=signal_issue)
        tracker.register_http(result, signal_issue=signal_issue)

        from xoso66_session import persist_session

        await asyncio.to_thread(persist_session, account_id, session)
        rounds.done(signal_issue)
    except Exception as e:
        rounds.pending.discard(signal_issue)
        print(f"[{_ts()}] CƯỢC LỖI issue={signal_issue}: {e}", flush=True)


def _handle_open_info(
    data: dict[str, Any],
    *,
    session: dict,
    account_id: str,
    amount: int,
    delay_min: float,
    delay_max: float,
    rounds: RoundState,
    tracker: BetTracker,
    rlog: CompactRoundLogger,
) -> bool:
    """open_info: KQ phiên cũ + next_info = bắt đầu phiên mới. Trả True nếu đã hẹn cược."""
    ended = str(data.get("issue") or "")
    if ended and ended in rlog._started:
        rlog.on_open_result(data, tracker)

    nxt = data.get("next_info")
    if not isinstance(nxt, dict):
        return False
    issue = str(nxt.get("issue") or "")
    if not issue or not rounds.accept_new_round(issue):
        return False

    begin_s = str(nxt.get("begin_time") or "")
    end_s = str(nxt.get("end_time") or "")
    rlog.on_round_start(
        issue,
        begin_time=begin_s,
        end_time=end_s,
        countdown=nxt.get("countdown"),
        from_next_info=True,
    )

    rem = _seconds_until(end_s)
    if rem is not None and rem < delay_min + 1.0:
        rlog.on_bet_skip(issue, f"hết cửa (còn {rem:.0f}s)")
        rounds.skip(issue)
        return False

    delay_sec = random.uniform(delay_min, delay_max)
    if rem is not None:
        delay_sec = min(delay_sec, max(0.5, rem - 2.0))

    asyncio.create_task(
        _bet_after_delay(
            session,
            account_id,
            issue,
            amount=amount,
            delay_sec=delay_sec,
            rounds=rounds,
            tracker=tracker,
            rlog=rlog,
        )
    )
    return True


async def run_ws_bet_test(
    account_id: str,
    *,
    amount: int = 1000,
    delay_min: float = BET_DELAY_MIN,
    delay_max: float = BET_DELAY_MAX,
    max_bets: int = 0,
) -> None:
    from xoso66_minigame_refresh import refresh_minigame_tokens
    from xoso66_minigame_session import get_ws_token
    from xoso66_proxy import ensure_proxy
    from xoso66_session import ensure_session

    session = ensure_session(account_id, force_login=False)
    ensure_proxy(session)
    rep = refresh_minigame_tokens(
        session, account_id=account_id, game_key=GAME_KEY, force=True
    )
    if not rep.get("ok"):
        raise SystemExit(f"Refresh token lỗi: {rep}")
    from xoso66_minigame_refresh import user_token_status

    st = user_token_status(session)
    print(
        f"user-token: ping_ok={st.get('ping_ok')} age_db={st.get('age_db_sec')} "
        f"max={st.get('max_age_sec')}s — kiểm tra: python xoso66_minigame_refresh.py -a {account_id} --check",
        flush=True,
    )

    user = session.get("username") or account_id
    print(
        f"=== WS → BET | {user} | {account_id} | {GAME_KEY} (id={GAME_ID}) ===",
        flush=True,
    )
    print(
        f"Mỗi phiên 5 dòng: BẮT ĐẦU → CƯỢC → KẾT QUẢ → THẮNG/THUA (WS balance) → JACKPOT. "
        f"Tín hiệu: open_info→BẮT ĐẦU PHIÊN (~30s), cược sau {delay_min:.0f}-{delay_max:.0f}s. Ctrl+C dừng.\n",
        flush=True,
    )

    rounds = RoundState()
    tracker = BetTracker()
    rlog = CompactRoundLogger(game_id=GAME_ID)
    bets_done = 0
    reconnect_delay = 5.0
    ws = None
    sock = None
    ping_stop = asyncio.Event()
    ping_task: asyncio.Task | None = None

    while True:
        if max_bets and bets_done >= max_bets:
            print(f"[{_ts()}] Đủ {max_bets} lần cược.", flush=True)
            break
        try:
            token = await asyncio.to_thread(
                get_ws_token,
                session,
                account_id,
                game_key=GAME_KEY,
                force_refresh=False,
            )
            ws_url = ws_url_from_token(token)
            ws, sock = await _connect_ws(ws_url, session["proxy"])
            ping_stop = asyncio.Event()
            ping_task = asyncio.create_task(ws_ping_loop(ws, GAME_ID, stop=ping_stop))
            await ws_send_subscribes(ws, [0, GAME_ID], verbose=False)
            print(f"[{_ts()}] WS connected", flush=True)

            while True:
                if max_bets and bets_done >= max_bets:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=45.0)
                except asyncio.TimeoutError:
                    continue

                obj = _decode_message(raw)
                if isinstance(obj, dict) and obj.get("_app_ping"):
                    await ws.send("pong")
                    continue
                if _is_ping_payload(obj):
                    reply = _pong_reply(obj)
                    if reply:
                        await ws.send(reply)
                    continue

                rlog.on_ws(obj, tracker)

                if not isinstance(obj, dict):
                    continue
                t = str(obj.get("type") or "").lower()
                if t == "logout":
                    raise ConnectionError(str(obj.get("msg") or "logout"))

                if t in ("g_open_info", "open_info"):
                    data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
                    if _watch_gid(data) != GAME_ID:
                        continue
                    if _handle_open_info(
                        data,
                        session=session,
                        account_id=account_id,
                        amount=amount,
                        delay_min=delay_min,
                        delay_max=delay_max,
                        rounds=rounds,
                        tracker=tracker,
                        rlog=rlog,
                    ):
                        bets_done += 1
                    continue

        except ConnectionError as e:
            print(f"[{_ts()}] WS mất: {e} — reconnect {reconnect_delay}s", flush=True)
            await asyncio.sleep(reconnect_delay)
        except Exception as e:
            print(f"[{_ts()}] Lỗi: {e} — reconnect {reconnect_delay}s", flush=True)
            await asyncio.sleep(reconnect_delay)
        finally:
            ping_stop.set()
            if ping_task is not None:
                ping_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await ping_task
            if ws is not None:
                with contextlib.suppress(Exception):
                    await ws.close()
            if sock is not None:
                with contextlib.suppress(Exception):
                    sock.close()
            ws = None
            sock = None
            ping_task = None
            ping_stop = asyncio.Event()


def main() -> int:
    ap = argparse.ArgumentParser(description="WS → bet module → log WS cược/thưởng")
    ap.add_argument("-u", "--username", default="", help="username trong DB")
    ap.add_argument("-a", "--account", default="", help="account id (acc1) — thay cho -u")
    ap.add_argument("--amount", type=int, default=1000)
    ap.add_argument("--delay-min", type=float, default=BET_DELAY_MIN)
    ap.add_argument("--delay-max", type=float, default=BET_DELAY_MAX)
    ap.add_argument("--max-bets", type=int, default=0)
    args = ap.parse_args()

    if not (args.username or "").strip() and not (args.account or "").strip():
        args.username = input("Username: ").strip()
    if not (args.username or "").strip() and not (args.account or "").strip():
        print("Cần -u username hoặc -a account.", file=sys.stderr)
        return 1

    account_id = resolve_account_id(username=args.username, account_id=args.account)
    if args.delay_max < args.delay_min:
        args.delay_max = args.delay_min

    try:
        asyncio.run(
            run_ws_bet_test(
                account_id,
                amount=args.amount,
                delay_min=args.delay_min,
                delay_max=args.delay_max,
                max_bets=args.max_bets,
            )
        )
    except KeyboardInterrupt:
        print(f"\n[{_ts()}] Dừng.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
