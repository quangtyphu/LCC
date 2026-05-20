# -*- coding: utf-8 -*-
"""
Đồng bộ lịch sử nạp/rút từ API paymentorderlist → SQLite (qua proxy acc).

  python xoso66_payment_history_sync.py -u chimtodang
  python xoso66_payment_history_sync.py --all --days 7
  python xoso66_payment_history_sync.py --all --delay-dw 10 --delay-acc 2
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from xoso66_accounts_db import (
    get_account,
    get_account_by_username,
    init_db,
    list_accounts,
    username_for_log,
)
from xoso66_deposit import list_payment_orders
from xoso66_payment_history_db import (
    ORDER_TYPE_DEPOSIT,
    ORDER_TYPE_WITHDRAW,
    init_payment_history_tables,
    list_payment_orders_db,
    sync_deposit_successes_from_list,
)
from xoso66_session import ensure_session
from xoso66_withdraw_tracking import sync_withdraw_list_to_db


def fetch_payment_orders_paged(
    session: dict,
    *,
    order_type: int,
    days: int = 7,
    status: str = "-1",
    page_limit: int = 50,
    max_pages: int = 50,
) -> tuple[list[dict[str, Any]], str]:
    """Lấy toàn bộ trang từ site. Trả (list, error)."""
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - max(1, int(days)) * 24 * 3600 * 1000
    all_items: list[dict[str, Any]] = []
    page = 1
    last_err = ""

    while page <= max_pages:
        rep = list_payment_orders(
            session=session,
            order_type=order_type,
            status=status,
            page=page,
            limit=page_limit,
            start_time_ms=start_ms,
            end_time_ms=now_ms,
        )
        if not rep.get("ok"):
            last_err = str(rep.get("msg") or rep.get("raw") or "API lỗi")
            if page == 1:
                return [], last_err
            break
        batch = rep.get("list") or []
        if not isinstance(batch, list):
            break
        all_items.extend(batch)
        data = rep.get("data") if isinstance(rep.get("data"), dict) else {}
        try:
            total = int(data.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        if len(batch) < page_limit:
            break
        if total and len(all_items) >= total:
            break
        page += 1

    return all_items, ""


def _proxy_line(session: dict, account_id: str) -> str:
    from xoso66_proxy import proxy_source_label

    row = get_account(account_id) or {}
    return proxy_source_label(session, account_proxy=str(row.get("proxy") or ""))


def sync_account_payment_history(
    account_id: str,
    *,
    days: int = 7,
    types: tuple[int, ...] = (ORDER_TYPE_DEPOSIT, ORDER_TYPE_WITHDRAW),
    session: dict | None = None,
    delay_deposit_withdraw_sec: float = 0,
    verbose: bool = True,
) -> dict[str, Any]:
    """Đồng bộ nạp rồi rút (có delay giữa hai bước), qua proxy acc."""
    aid = str(account_id).strip()
    row = get_account(aid)
    if not row:
        return {"ok": False, "account_id": aid, "error": "không tìm thấy account"}

    u = username_for_log(aid, row)

    if session is None:
        try:
            session = ensure_session(aid, force_login=False)
        except Exception as e:
            return {"ok": False, "account_id": aid, "username": u, "error": str(e)}

    out: dict[str, Any] = {
        "ok": True,
        "account_id": aid,
        "username": u,
        "days": int(days),
        "deposit": 0,
        "withdraw": 0,
        "deposit_new": 0,
        "withdraw_new": 0,
        "withdraw_status_changed": 0,
        "deposit_new_serials": [],
        "withdraw_new_serials": [],
        "has_new": False,
        "errors": [],
    }

    if verbose:
        print(f"  [{u}] {_proxy_line(session, aid)}", flush=True)

    do_dep = ORDER_TYPE_DEPOSIT in types
    do_wd = ORDER_TYPE_WITHDRAW in types

    if do_dep:
        if verbose:
            print(f"  [{u}] nạp — paymentorderlist ({days} ngày)…", flush=True)
        items, err = fetch_payment_orders_paged(
            session, order_type=ORDER_TYPE_DEPOSIT, days=days
        )
        if err and not items:
            out["errors"].append(f"deposit: {err}")
        else:
            dep_sync = sync_deposit_successes_from_list(aid, items)
            out["deposit_sync"] = dep_sync
            out["deposit_new"] = int(dep_sync.get("count_new") or 0)
            out["deposit_new_serials"] = list(dep_sync.get("new_serials") or [])
            out["deposit"] = len(items)
            if err:
                out["errors"].append(f"deposit (một phần): {err}")
            if verbose:
                print(
                    f"  [{u}] nạp: {len(items)} dòng site, "
                    f"+{out['deposit_new']} mới DB",
                    flush=True,
                )

    if do_dep and do_wd and delay_deposit_withdraw_sec > 0:
        if verbose:
            print(
                f"  [{u}] chờ {delay_deposit_withdraw_sec:.0f}s trước check rút…",
                flush=True,
            )
        time.sleep(delay_deposit_withdraw_sec)

    if do_wd:
        if verbose:
            print(f"  [{u}] rút — paymentorderlist ({days} ngày)…", flush=True)
        items, err = fetch_payment_orders_paged(
            session, order_type=ORDER_TYPE_WITHDRAW, days=days
        )
        if err and not items:
            out["errors"].append(f"withdraw: {err}")
        else:
            wd_sync = sync_withdraw_list_to_db(aid, items)
            ss = wd_sync.get("success_sync") or {}
            wd_new_rows = list(wd_sync.get("new_serials") or [])
            out["withdraw_sync"] = wd_sync
            out["withdraw_new"] = len(wd_new_rows)
            out["withdraw_new_serials"] = wd_new_rows
            out["withdraw_hoan_tat_new"] = int(ss.get("count_new") or 0)
            out["withdraw_status_changed"] = len(wd_sync.get("status_changed") or [])
            out["withdraw"] = len(items)
            if err:
                out["errors"].append(f"withdraw (một phần): {err}")
            if verbose:
                print(
                    f"  [{u}] rút: {len(items)} dòng site, "
                    f"+{len(wd_new_rows)} mới DB, "
                    f"{out['withdraw_status_changed']} đổi trạng thái",
                    flush=True,
                )

    out["has_new"] = (
        out["deposit_new"] > 0
        or len(out["withdraw_new_serials"]) > 0
        or out["withdraw_status_changed"] > 0
    )
    out["ok"] = not out["errors"] or (out["deposit"] + out["withdraw"] > 0)
    return out


def sync_all_accounts(
    *,
    days: int = 7,
    account_status: str | None = None,
    delay_deposit_withdraw_sec: float = 10,
    delay_between_accounts_sec: float = 2,
    verbose: bool = True,
) -> list[dict[str, Any]]:
    rows = list_accounts()
    if account_status:
        st = str(account_status).strip()
        rows = [r for r in rows if str(r.get("status") or "") == st]
    results: list[dict[str, Any]] = []
    total = len(rows)
    for i, row in enumerate(rows, 1):
        aid = str(row.get("id") or "")
        if not aid:
            continue
        u = username_for_log(aid, row)
        if verbose:
            print(f"\n[{i}/{total}] {u}", flush=True)
        results.append(
            sync_account_payment_history(
                aid,
                days=days,
                delay_deposit_withdraw_sec=delay_deposit_withdraw_sec,
                verbose=verbose,
            )
        )
        if i < total and delay_between_accounts_sec > 0:
            time.sleep(delay_between_accounts_sec)
    return results


def print_new_accounts_summary(results: list[dict[str, Any]]) -> None:
    """In danh sách acc có GD mới / lỗi."""
    with_new: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for r in results:
        if not r.get("ok"):
            failed.append(r)
        elif r.get("has_new"):
            with_new.append(r)

    print("\n" + "=" * 60, flush=True)
    print("ACC CÓ LỊCH SỬ MỚI / CẬP NHẬT DB", flush=True)
    print("=" * 60, flush=True)
    if not with_new:
        print("(không có acc nào có giao dịch mới)", flush=True)
    else:
        for r in with_new:
            u = r.get("username") or r.get("account_id")
            parts = []
            dn = int(r.get("deposit_new") or 0)
            wn = len(r.get("withdraw_new_serials") or [])
            sc = int(r.get("withdraw_status_changed") or 0)
            if dn:
                parts.append(f"nạp +{dn}")
            if wn:
                parts.append(f"rút +{wn} mới")
            if sc:
                parts.append(f"rút {sc} đổi TT")
            print(f"  • {u}: {', '.join(parts)}", flush=True)
            for s in (r.get("deposit_new_serials") or [])[:3]:
                print(f"      nạp serial {s}", flush=True)
            for s in (r.get("withdraw_new_serials") or [])[:3]:
                print(f"      rút serial {s}", flush=True)

    print(
        f"\nTổng: {len(with_new)}/{len(results)} acc có GD mới/cập nhật",
        flush=True,
    )
    if failed:
        print(f"Lỗi: {len(failed)} acc", flush=True)
        for r in failed:
            u = r.get("username") or r.get("account_id")
            err = r.get("error") or r.get("errors")
            print(f"  ✗ {u}: {err}", flush=True)


def resolve_account_id(account_id: str = "", username: str = "") -> str:
    """account id hoặc username → id trong DB."""
    for key in (str(username or "").strip(), str(account_id or "").strip()):
        if not key:
            continue
        row = get_account(key) or get_account_by_username(key)
        if row:
            return str(row["id"])
    label = str(username or account_id or "").strip() or "?"
    raise SystemExit(f"Không tìm thấy account: {label}")


def _main() -> int:
    ap = argparse.ArgumentParser(description="Đồng bộ lịch sử nạp/rút XOSO66 → DB")
    ap.add_argument(
        "-a",
        "--account",
        help="account id hoặc username (vd: acc1, chimtodang)",
    )
    ap.add_argument("-u", "--username", help="username (ưu tiên nếu có cả -a)")
    ap.add_argument("--all", action="store_true", help="mọi account trong DB")
    ap.add_argument("--days", type=int, default=7, help="số ngày lùi (mặc định 7)")
    ap.add_argument(
        "--status",
        default="",
        help="lọc status account khi --all (vd: Đang Chơi)",
    )
    ap.add_argument(
        "--delay-dw",
        type=float,
        default=10,
        help="giây chờ giữa check nạp và check rút (mặc định 10)",
    )
    ap.add_argument(
        "--delay-acc",
        type=float,
        default=2,
        help="giây chờ giữa từng account khi --all (mặc định 2)",
    )
    ap.add_argument("--list", action="store_true", help="in DB sau sync")
    ap.add_argument("--type", type=int, default=0, help="1=nạp 2=rút 0=cả hai (chỉ --list)")
    args = ap.parse_args()

    init_db()
    init_payment_history_tables()

    if not args.all and not args.account and not args.username:
        try:
            args.username = input("Username: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("", flush=True)
            ap.error("Cần -u/--username, -a hoặc --all")

    if args.list:
        if not args.account and not args.username:
            ap.error("--list cần -u hoặc -a")
        aid = resolve_account_id(args.account or "", args.username or "")
        ot = int(args.type) if args.type in (1, 2) else None
        data = list_payment_orders_db(aid, order_type=ot, limit=30)
        uname = (data["list"][0].get("username") if data["list"] else "") or aid
        print(f"DB {uname}: total={data['total']}", flush=True)
        for row in data["list"]:
            u = row.get("username") or uname
            print(
                f"  [{u}] [{row['order_type']}] {row['create_time']} "
                f"{row['true_amount']:,.0f} — {row['status_formatted']} "
                f"{row['serial_no']}",
                flush=True,
            )
        return 0

    if args.all:
        print(
            f"[SYNC-ALL] {len(list_accounts())} acc trong DB"
            + (f', lọc status="{args.status}"' if args.status else "")
            + f" | {args.days} ngày | delay nạp→rút {args.delay_dw}s"
            + f" | delay acc {args.delay_acc}s | proxy acc",
            flush=True,
        )
        reps = sync_all_accounts(
            days=args.days,
            account_status=args.status or None,
            delay_deposit_withdraw_sec=max(0, float(args.delay_dw)),
            delay_between_accounts_sec=max(0, float(args.delay_acc)),
        )
        ok = sum(1 for r in reps if r.get("ok"))
        print(f"\n[SYNC-ALL] Xong — OK {ok}/{len(reps)} acc", flush=True)
        print_new_accounts_summary(reps)
        return 0 if ok == len(reps) else 1

    if not args.account and not args.username:
        ap.error("Cần -u/--username, -a hoặc --all")
    aid = resolve_account_id(args.account or "", args.username or "")
    rep = sync_account_payment_history(
        aid,
        days=args.days,
        delay_deposit_withdraw_sec=max(0, float(args.delay_dw)),
    )
    print(json_dumps(rep), flush=True)
    if rep.get("has_new"):
        print_new_accounts_summary([rep])
    return 0 if rep.get("ok") else 1


def json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    raise SystemExit(_main())
