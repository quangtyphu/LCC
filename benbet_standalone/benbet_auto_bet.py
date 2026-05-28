# -*- coding: utf-8 -*-
"""
Tự động cược Tài Xỉu Cân Bảng — random cửa / tiền, cược khi Ellapsed 35–45s, giới hạn tổng cược ngày.

  python benbet_auto_bet.py
  python benbet_auto_bet.py -u USER -p PASS --proxy host:port:user:pass --daily 500000
"""
from __future__ import annotations

import argparse
import random
import sys

from benbet_login import login
from benbet_proxy import proxy_label, require_socks_deps
from benbet_taixiu_ws import (
    BET_ELAPSED_MAX,
    BET_ELAPSED_MIN,
    MIN_BET,
    SIDE_TAI,
    SIDE_XIU,
    client_from_login,
    side_label,
)

# Đơn vị wire = VND; app hiển thị ÷1000
BET_UNIT = 1_000  # tiền cược thực tế phải bội 1k (không 28.562)
BET_RANDOM_STEP = 20_000  # random chọn 20k / 30k / 40k / 50k
BET_MIN = 20_000
BET_MAX = 100_000
WAIT_MIN = 0.0
WAIT_MAX = 0.0  # không jitter thêm — chờ Ellapsed trong [35, 45] rồi cược ngay
WIN_PAYOUT_MULT = 1.98  # thắng: số dư sau cược + tiền cược × 1.98


def _fix_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _fmt_money(amount: int) -> str:
    return f"{amount:,}".replace(",", ".")


def balance_from_login(user_info: dict) -> int:
    v = user_info.get("amount_vnd")
    if v is not None and str(v).strip():
        return int(float(str(v).replace(",", "").replace(".", "")))
    amt = user_info.get("amount")
    if amt is not None:
        return int(float(amt))
    return 0


def _bet_units(value: int) -> int:
    """Làm tròn xuống bội 1k — số tiền gửi lên server."""
    if value <= 0:
        return 0
    return (value // BET_UNIT) * BET_UNIT


def _random_bet_pick() -> int:
    """Random 20k, 30k, 40k hoặc 50k (bội 10k)."""
    n = random.randint(BET_MIN // BET_RANDOM_STEP, BET_MAX // BET_RANDOM_STEP)
    return n * BET_RANDOM_STEP


def compute_bet_amount(balance: int, daily_left: int) -> int:
    """
    Random 20–50k (bội 10k). Tiền cược thực tế bội 1k.
    Nếu sau cược còn < 20k → cược hết bội 1k (VD 28.562, random 20k → cược 28k).
    """
    if balance < MIN_BET or daily_left <= 0:
        return 0

    bal = _bet_units(balance)
    left = _bet_units(daily_left)
    if bal < MIN_BET:
        return 0

    if left <= BET_MIN:
        amount = _bet_units(min(BET_MIN, left, bal))
    else:
        amount = _random_bet_pick()
        amount = min(amount, left, bal)

    # Số dư sau cược < 20k → cược hết (bội 1k): 28.562 + random 20k → 28.000
    if bal - amount < BET_MIN:
        amount = bal

    amount = min(_bet_units(amount), bal, left)
    if amount < MIN_BET:
        return 0
    return int(amount)


def parse_daily_input(raw: str) -> int:
    s = raw.strip().replace(".", "").replace(",", "")
    return int(s)


def run_auto_bet(
    username: str,
    password: str,
    daily_limit: int,
    *,
    proxy: str,
    wait_min: float = WAIT_MIN,
    wait_max: float = WAIT_MAX,
) -> int:
    _fix_stdout()
    if daily_limit < BET_MIN:
        print(f"Tổng cược ngày phải >= {_fmt_money(BET_MIN)}")
        return 1

    try:
        require_socks_deps(for_websocket=True)
    except RuntimeError as exc:
        print(exc)
        return 1

    lg = login(username, password, proxy=proxy)
    if not lg.get("ok"):
        print(f"Đăng nhập thất bại: {lg.get('message')}")
        return 1

    balance = balance_from_login(lg.get("user_info") or {})
    print(f"Proxy: {proxy_label(proxy)}", flush=True)
    print(f"Đăng nhập OK — số dư ~ {_fmt_money(balance)}", flush=True)
    print(f"Mục tiêu tổng cược ngày: {_fmt_money(daily_limit)}", flush=True)
    print("-" * 50, flush=True)

    try:
        client = client_from_login(username, password, quiet=True, proxy=proxy)
    except Exception as exc:
        print(f"Không mở được WS: {exc}")
        return 1

    ws_thread = client.start_background_reconnect(
        username=username,
        password=password,
        proxy=proxy,
    )

    daily_staked = 0
    round_no = 0
    interrupted = False
    try:
        if not client._wait_connected(15.0):
            print("Không kết nối được WS trong 15s.", flush=True)
            return 1
        if client.balance is not None:
            balance = client.balance

        last_session_id = 0

        while not interrupted and daily_staked < daily_limit:
            if client.stopping():
                interrupted = True
                break

            if not client.wait_next_bet_window(
                last_session_id,
                min_elapsed=BET_ELAPSED_MIN,
                max_elapsed=BET_ELAPSED_MAX,
                timeout=120,
            ):
                if client.stopping():
                    interrupted = True
                else:
                    print("Bỏ qua: hết thời gian chờ phiên cược mới.", flush=True)
                break

            info = client.session_info or {}
            bet_session_id = client._session_id_from(info)

            # Random một mốc trong [35, 45] rồi chờ Ellapsed đếm tới mốc đó
            target_elapsed = random.randint(BET_ELAPSED_MIN, BET_ELAPSED_MAX)
            while True:
                if client.stopping():
                    interrupted = True
                    break
                info = client.session_info or {}
                try:
                    cur = int(info.get("Ellapsed") or 0)
                except (TypeError, ValueError):
                    cur = 0
                if cur <= target_elapsed:
                    break
                if cur < BET_ELAPSED_MIN:
                    break
                if not client._sleep(0.2):
                    interrupted = True
                    break
            if interrupted:
                break

            if wait_max > wait_min:
                jitter = random.uniform(wait_min, wait_max)
                info = client.session_info or {}
                try:
                    cur = int(info.get("Ellapsed") or 0)
                except (TypeError, ValueError):
                    cur = BET_ELAPSED_MAX
                cap = max(0.0, cur - BET_ELAPSED_MIN)
                jitter = min(jitter, cap)
                if jitter > 0 and not client._sleep(jitter):
                    interrupted = True
                    break

            info = client.session_info or {}
            bet_session_id = client._session_id_from(info)
            try:
                elapsed = int(info.get("Ellapsed") or 0)
            except (TypeError, ValueError):
                elapsed = 0

            if elapsed < BET_ELAPSED_MIN or elapsed > BET_ELAPSED_MAX:
                sid = bet_session_id or "?"
                print(
                    f"Bỏ qua phiên {sid}: Ellapsed={elapsed} "
                    f"(cần {BET_ELAPSED_MIN}–{BET_ELAPSED_MAX}).",
                    flush=True,
                )
                if bet_session_id is not None:
                    last_session_id = bet_session_id
                continue

            daily_left = daily_limit - daily_staked
            if client.balance is not None:
                balance = client.balance

            amount = compute_bet_amount(balance, daily_left)
            if amount < MIN_BET:
                print(
                    f"Dừng: số dư {_fmt_money(balance)} (< {MIN_BET // 1000}k server), "
                    f"còn quota {_fmt_money(daily_left)}.",
                    flush=True,
                )
                break

            side = random.choice([SIDE_TAI, SIDE_XIU])
            label = side_label(side)
            round_no += 1

            info = client.session_info or {}
            try:
                elapsed = int(info.get("Ellapsed") or elapsed)
            except (TypeError, ValueError):
                pass

            try:
                client.bet(side, amount)
            except Exception as exc:
                print(f"Lỗi gửi cược: {exc}", flush=True)
                continue

            if not client.wait_bet_success(12):
                if client.stopping():
                    interrupted = True
                    break
                err = client._bet_err or "không có betSuccess"
                print(f"Cược thất bại: {err}", flush=True)
                continue

            if bet_session_id is not None:
                last_session_id = bet_session_id

            daily_staked += amount
            # betSuccess A: [ {Bet...}, balance_sau_cuoc, BetValue ]
            bs = client.last_bet_success or {}
            args = bs.get("A") or []
            if len(args) > 1 and isinstance(args[1], (int, float)):
                balance_after_bet = int(args[1])
            elif client.balance is not None:
                balance_after_bet = client.balance
            else:
                balance_after_bet = balance
            balance = balance_after_bet
            client.balance = balance_after_bet

            print(
                f"Đã đặt {label} {_fmt_money(amount)} "
                f"(phiên {bet_session_id}, Ellapsed={elapsed}) "
                f"-------- Số dư còn lại {_fmt_money(balance_after_bet)}",
                flush=True,
            )
            print(f"Tổng cược {_fmt_money(daily_staked)}", flush=True)

            outcome = (
                client.wait_round_outcome(last_session_id, timeout=75)
                if last_session_id
                else None
            )
            if client.stopping():
                interrupted = True
                break
            if outcome is None:
                if not client.is_connected():
                    print(
                        "Mất WS khi chờ kết quả — đã/đang tự reconnect, bỏ qua phiên này.",
                        flush=True,
                    )
                else:
                    print("Không nhận sessionResult phiên vừa cược.", flush=True)
            elif outcome.get("won"):
                balance_after_win = int(balance_after_bet + amount * WIN_PAYOUT_MULT)
                balance = balance_after_win
                client.balance = balance_after_win
                print(
                    f"Kết quả: Thắng — số dư sau khi thắng {_fmt_money(balance_after_win)} "
                    f"({_fmt_money(balance_after_bet)} + {_fmt_money(amount)}×{WIN_PAYOUT_MULT})",
                    flush=True,
                )
            else:
                print("Kết quả: Thua / không thưởng", flush=True)

            print("-" * 50, flush=True)

            if daily_staked >= daily_limit:
                break

    except KeyboardInterrupt:
        interrupted = True
        print("\nĐã dừng (Ctrl+C).", flush=True)
    finally:
        client.close()
        ws_thread.join(timeout=3.0)

    print(
        f"\n{'Dừng' if interrupted else 'Xong'} — {round_no} lần cược, "
        f"tổng đã cược: {_fmt_money(daily_staked)} / {_fmt_money(daily_limit)}",
        flush=True,
    )
    return 130 if interrupted else 0


def main(argv: list[str] | None = None) -> int:
    _fix_stdout()
    ap = argparse.ArgumentParser(description="Auto cược Tài Xỉu Benbet")
    ap.add_argument("-u", "--username", default="")
    ap.add_argument("-p", "--password", default="")
    ap.add_argument("--daily", type=int, default=0, help="Tổng cược ngày (VND)")
    ap.add_argument(
        "--proxy",
        default="",
        help="SOCKS5 host:port hoặc host:port:user:pass",
    )
    args = ap.parse_args(argv)

    username = args.username.strip()
    password = args.password.strip()
    daily = args.daily
    proxy = (args.proxy or "").strip()

    if not username:
        username = input("Username: ").strip()
    if not password:
        password = input("Password: ").strip()
    if daily <= 0:
        daily = parse_daily_input(
            input("Tổng cược ngày (VND, VD 500000): ").strip()
        )
    if not proxy:
        proxy = input("SOCKS5 proxy (host:port hoặc host:port:user:pass): ").strip()
    if not proxy:
        print("Cần proxy SOCKS5 — login, get_url, negotiate và WS đều qua proxy.")
        return 1

    return run_auto_bet(username, password, daily, proxy=proxy)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nĐã dừng (Ctrl+C).", flush=True)
        raise SystemExit(130)
