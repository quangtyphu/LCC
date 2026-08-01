#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Quét tài khoản có lịch sử cược Xổ số (tab winlossHistory).

API: GET /server/order/historywinlosereport?start_date=&end_date=
Tiêu chí «có»: total_sum.valid_amount_total > 0 (hoặc bet_count_total > 0).

  python xoso66_check_lottery_winloss.py -u hainam19891 --days 7
  python xoso66_check_lottery_winloss.py --all --days 7
  python xoso66_check_lottery_winloss.py --all --days 7 -j 16
  python xoso66_check_lottery_winloss.py --all --days 7 --out lottery_winloss_hits.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from xoso66_paths import apply_default_env

apply_default_env()

from xoso66_accounts_db import (  # noqa: E402
    get_account,
    get_account_by_username,
    init_db,
    list_accounts,
    list_accounts_by_status,
    username_for_log,
)
from xoso66_config_util import configure_stdio_utf8  # noqa: E402
from xoso66_deposit import (  # noqa: E402
    apply_response_tokens,
    build_common_headers,
    get_form_token,
)
from xoso66_session import (  # noqa: E402
    BASE_URL,
    _merge_response_cookies,
    _requests_session,
    ensure_session,
    persist_session,
)

configure_stdio_utf8()

_PRINT_LOCK = threading.Lock()

HISTORY_WINLOSS_PATH = "/server/order/historywinlosereport"
VN_TZ = timezone(timedelta(hours=7))
_DIR = Path(__file__).resolve().parent


def winloss_date_range_ms(days: int = 7) -> tuple[int, int]:
    """start = 00:00 VN của (hôm nay - days + 1); end = 23:59:59.999 VN hôm nay."""
    d = max(1, int(days))
    now = datetime.now(VN_TZ)
    end_dt = now.replace(hour=23, minute=59, second=59, microsecond=999000)
    start_day = (now.date() - timedelta(days=d - 1))
    start_dt = datetime(
        start_day.year,
        start_day.month,
        start_day.day,
        0,
        0,
        0,
        0,
        tzinfo=VN_TZ,
    )
    return int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)


def _to_float(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def fetch_lottery_winloss_report(
    session: dict,
    *,
    days: int = 7,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> dict[str, Any]:
    """
    GET /server/order/historywinlosereport — lịch sử thắng thua tab Xổ số.
    Trả ok + list + totals (bet_count, valid_amount, win_lose).
    """
    if start_ms is None or end_ms is None:
        start_ms, end_ms = winloss_date_range_ms(days)

    form_token = get_form_token(session)
    headers = build_common_headers(
        session,
        form_token=form_token,
        content_type="application/x-www-form-urlencoded/json",
    )
    url = f"{BASE_URL}{HISTORY_WINLOSS_PATH}"
    params = {"start_date": int(start_ms), "end_date": int(end_ms)}
    r = _requests_session(session).get(url, headers=headers, params=params, timeout=35)
    apply_response_tokens(session, r.headers)
    _merge_response_cookies(session, r)
    try:
        js = r.json()
    except Exception:
        return {
            "ok": False,
            "http_status": r.status_code,
            "error": "JSON lỗi",
            "raw": (r.text or "")[:500],
        }

    ok = r.status_code == 200 and js.get("code") == 1
    data = js.get("data") if isinstance(js.get("data"), dict) else {}
    total_sum = data.get("total_sum") if isinstance(data.get("total_sum"), dict) else {}
    items = data.get("list") if isinstance(data.get("list"), list) else []

    bet_count = _to_float(total_sum.get("bet_count_total"))
    total_bet = _to_float(total_sum.get("valid_amount_total"))
    total_winloss = _to_float(total_sum.get("win_lose_total"))

    # Fallback: cộng từ list nếu total_sum thiếu
    if not total_sum and items:
        bet_count = sum(_to_float(x.get("bet_count")) for x in items if isinstance(x, dict))
        total_bet = sum(_to_float(x.get("valid_amount")) for x in items if isinstance(x, dict))
        total_winloss = sum(_to_float(x.get("win_lose")) for x in items if isinstance(x, dict))

    return {
        "ok": ok,
        "http_status": r.status_code,
        "code": js.get("code"),
        "msg": js.get("msg"),
        "list": items,
        "total_sum": total_sum,
        "bet_count": bet_count,
        "total_bet": total_bet,
        "total_winloss": total_winloss,
        "has_lottery_bets": total_bet > 0 or bet_count > 0,
        "start_ms": int(start_ms),
        "end_ms": int(end_ms),
        "raw": js if not ok else None,
        "error": None if ok else str(js.get("msg") or f"code={js.get('code')}"),
    }


def resolve_account_id(account_or_username: str) -> str | None:
    key = str(account_or_username or "").strip()
    if not key:
        return None
    if get_account(key):
        return key
    acc = get_account_by_username(key)
    if acc:
        return str(acc.get("id") or "")
    return None


def check_account_lottery_winloss(
    account_or_username: str,
    *,
    days: int = 7,
    force_login: bool = False,
    verbose: bool = True,
) -> dict[str, Any]:
    aid = resolve_account_id(account_or_username)
    if not aid:
        return {
            "ok": False,
            "error": f"không tìm thấy account: {account_or_username!r}",
        }

    row = get_account(aid) or {}
    user = username_for_log(aid, row)

    try:
        session = ensure_session(aid, force_login=force_login)
    except Exception as e:
        return {
            "ok": False,
            "account_id": aid,
            "username": user,
            "status": str(row.get("status") or ""),
            "error": f"session: {e}",
        }

    rep = fetch_lottery_winloss_report(session, days=days)
    try:
        persist_session(aid, session)
    except Exception:
        pass

    out: dict[str, Any] = {
        "ok": bool(rep.get("ok")),
        "account_id": aid,
        "username": user,
        "status": str(row.get("status") or ""),
        "days": int(days),
        "bet_count": rep.get("bet_count", 0),
        "total_bet": rep.get("total_bet", 0),
        "total_winloss": rep.get("total_winloss", 0),
        "has_lottery_bets": bool(rep.get("has_lottery_bets")),
        "error": rep.get("error"),
    }
    if verbose:
        with _PRINT_LOCK:
            if not out["ok"]:
                print(f"❌ [{user}] {out.get('error')}", flush=True)
            elif out["has_lottery_bets"]:
                print(
                    f"✅ [{user}] có Xổ số — cược {int(out['total_bet']):,} | "
                    f"thắng/thua {int(out['total_winloss']):,} | "
                    f"{int(out['bet_count'])} lần",
                    flush=True,
                )
        # zero-bet: im lặng — chỉ in hits / lỗi
    return out


def scan_all_lottery_winloss(
    *,
    days: int = 7,
    status_filter: str = "",
    delay_acc: float = 0.0,
    force_login: bool = False,
    verbose: bool = True,
    workers: int = 16,
) -> dict[str, Any]:
    init_db()
    st = str(status_filter or "").strip()
    if st:
        accounts = list_accounts_by_status(st)
    else:
        accounts = list_accounts()

    hits: list[dict[str, Any]] = []
    zeros: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    total = len(accounts)
    n_workers = max(1, min(32, int(workers or 16)))
    if verbose:
        print(
            f"🔎 Quét {total} acc — Xổ số {days} ngày — song song {n_workers} luồng"
            + (f" (status={st})" if st else ""),
            flush=True,
        )

    def _one(row: dict[str, Any]) -> dict[str, Any]:
        aid = str(row.get("id") or "")
        if delay_acc > 0:
            time.sleep(float(delay_acc))
        return check_account_lottery_winloss(
            aid,
            days=days,
            force_login=force_login,
            verbose=verbose,
        )

    done = 0
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futs = {pool.submit(_one, row): row for row in accounts}
        for fut in as_completed(futs):
            done += 1
            try:
                rep = fut.result()
            except Exception as e:
                row = futs[fut]
                rep = {
                    "ok": False,
                    "account_id": str(row.get("id") or ""),
                    "username": str(row.get("username") or ""),
                    "error": str(e),
                }
            if not rep.get("ok"):
                failed.append(rep)
            elif rep.get("has_lottery_bets"):
                hits.append(rep)
            else:
                zeros.append(rep)
            if verbose and (done % 10 == 0 or done == total):
                with _PRINT_LOCK:
                    print(
                        f"… {done}/{total} xong "
                        f"(hits={len(hits)}, lỗi={len(failed)})",
                        flush=True,
                    )

    hits.sort(key=lambda r: str(r.get("username") or "").lower())
    failed.sort(key=lambda r: str(r.get("username") or "").lower())

    return {
        "ok": True,
        "days": days,
        "scanned": total,
        "workers": n_workers,
        "hits": hits,
        "zeros": zeros,
        "failed": failed,
    }


def _write_csv(path: Path, rows: list[dict[str, Any]], days: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "username",
        "account_id",
        "status",
        "total_bet",
        "total_winloss",
        "bet_count",
        "days",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    "username": r.get("username") or "",
                    "account_id": r.get("account_id") or "",
                    "status": r.get("status") or "",
                    "total_bet": int(_to_float(r.get("total_bet"))),
                    "total_winloss": int(_to_float(r.get("total_winloss"))),
                    "bet_count": int(_to_float(r.get("bet_count"))),
                    "days": int(r.get("days") or days),
                }
            )


def _print_summary(result: dict[str, Any]) -> None:
    hits = result.get("hits") or []
    zeros = result.get("zeros") or []
    failed = result.get("failed") or []
    days = int(result.get("days") or 7)
    print("", flush=True)
    print(
        f"=== Kết quả: {len(hits)} có cược Xổ số / "
        f"{result.get('scanned', 0)} đã quét "
        f"({len(zeros)} không có, {len(failed)} lỗi) — {days} ngày ===",
        flush=True,
    )
    if hits:
        print(f"{'username':<24} {'status':<12} {'cược':>12} {'thắng/thua':>12} {'lần':>6}", flush=True)
        for r in hits:
            print(
                f"{str(r.get('username') or ''):<24} "
                f"{str(r.get('status') or ''):<12} "
                f"{int(_to_float(r.get('total_bet'))):>12,} "
                f"{int(_to_float(r.get('total_winloss'))):>12,} "
                f"{int(_to_float(r.get('bet_count'))):>6}",
                flush=True,
            )
    if failed:
        print(f"\nLỗi ({len(failed)}):", flush=True)
        for r in failed:
            print(
                f"  ✗ {r.get('username') or r.get('account_id')}: {r.get('error')}",
                flush=True,
            )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Quét tài khoản có lịch sử cược Xổ số (historywinlosereport)"
    )
    ap.add_argument("-a", "--account", help="account id hoặc username")
    ap.add_argument("-u", "--username", help="username")
    ap.add_argument("--all", action="store_true", help="mọi account trong DB")
    ap.add_argument("--days", type=int, default=7, help="số ngày (mặc định 7)")
    ap.add_argument(
        "--status",
        default="",
        help="lọc status khi --all (vd: Đang Chơi)",
    )
    ap.add_argument(
        "--delay-acc",
        type=float,
        default=0.0,
        help="giây chờ trước mỗi acc (hiếm khi cần; mặc định 0)",
    )
    ap.add_argument(
        "-j",
        "--workers",
        type=int,
        default=16,
        help="số luồng song song khi --all (mặc định 16, max 32)",
    )
    ap.add_argument("--force-login", action="store_true")
    ap.add_argument(
        "--out",
        default="",
        help="đường dẫn CSV hits (mặc định game_data/lottery_winloss_hits.csv khi --all)",
    )
    ap.add_argument("--json", action="store_true", help="in JSON một acc")
    args = ap.parse_args()

    init_db()
    days = max(1, int(args.days))

    if args.all:
        result = scan_all_lottery_winloss(
            days=days,
            status_filter=args.status,
            delay_acc=float(args.delay_acc),
            force_login=bool(args.force_login),
            workers=int(args.workers),
            verbose=True,
        )
        _print_summary(result)
        out_path = Path(args.out.strip()) if str(args.out or "").strip() else None
        if out_path is None:
            from xoso66_paths import cms_game_data_dir

            out_path = cms_game_data_dir() / "lottery_winloss_hits.csv"
        _write_csv(out_path, result.get("hits") or [], days)
        print(f"\n💾 CSV: {out_path}", flush=True)
        return 0 if not (result.get("failed") or []) else 2

    key = (args.username or args.account or "").strip()
    if not key:
        try:
            key = input("Username / account_id: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("", flush=True)
            ap.error("Cần -u/--username, -a hoặc --all")
    if not key:
        ap.error("Cần -u/--username, -a hoặc --all")

    rep = check_account_lottery_winloss(
        key,
        days=days,
        force_login=bool(args.force_login),
        verbose=not args.json,
    )
    if args.json:
        import json

        print(json.dumps(rep, ensure_ascii=False, indent=2), flush=True)
    elif rep.get("ok") and rep.get("has_lottery_bets") and args.out:
        _write_csv(Path(args.out), [rep], days)
        print(f"💾 CSV: {args.out}", flush=True)
    return 0 if rep.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
