# -*- coding: utf-8 -*-
"""
Định kỳ check số dư API cho acc «Đang Chơi / Đủ ngày / Hết Tiền».

Mỗi chu kỳ:
  1. Lấy acc theo 3 status
  2. getBalance → cập nhật DB (tin số dư API)
  3. Nếu số dư thực tế < số dư DB và chênh lệch > ngưỡng → Telegram + gọi
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from xoso66_accounts_db import (
    STATUS_DANG_CHOI,
    STATUS_DU_NGAY,
    STATUS_HET_TIEN,
    get_account,
    list_accounts_by_status,
    username_for_log,
)
from xoso66_config_util import load_config
from xoso66_shutdown import stopping

_TARGET_STATUSES = (STATUS_DANG_CHOI, STATUS_DU_NGAY, STATUS_HET_TIEN)


def _cfg(cfg: dict | None = None) -> dict[str, Any]:
    raw = (cfg or load_config()).get("balance_reconcile")
    return raw if isinstance(raw, dict) else {}


def balance_reconcile_enabled(cfg: dict | None = None) -> bool:
    return bool(_cfg(cfg).get("enabled", True))


def _interval_min(cfg: dict | None = None) -> int:
    return max(1, int(_cfg(cfg).get("interval_min") or 60))


def _parallel(cfg: dict | None = None) -> int:
    return max(1, min(64, int(_cfg(cfg).get("parallel") or 12)))


def _min_drop_notify_vnd(cfg: dict | None = None) -> float:
    """Chỉ báo Telegram/gọi khi |chênh lệch| > ngưỡng này (mặc định 50_000)."""
    try:
        return max(0.0, float(_cfg(cfg).get("min_drop_notify_vnd", 50_000)))
    except (TypeError, ValueError):
        return 50_000.0


def list_target_accounts() -> list[dict[str, Any]]:
    """Union acc theo 3 status (không trùng id)."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for st in _TARGET_STATUSES:
        for row in list_accounts_by_status(st):
            aid = str(row.get("id") or "").strip()
            if not aid or aid in seen:
                continue
            seen.add(aid)
            out.append(row)
    return out


def _fmt_vnd(v: float) -> str:
    return f"{v:,.0f}"


def _reconcile_one(account_id: str, *, cfg: dict | None) -> dict[str, Any]:
    aid = str(account_id).strip()
    row = get_account(aid) or {}
    user = username_for_log(aid, row)
    status = str(row.get("status") or "").strip()
    try:
        db_balance = float(row.get("balance") or 0)
    except (TypeError, ValueError):
        db_balance = 0.0

    try:
        from xoso66_session import refresh_account_balance_to_db

        rep = refresh_account_balance_to_db(aid, refresh=True)
    except Exception as e:
        return {
            "ok": False,
            "account_id": aid,
            "username": user,
            "status": status,
            "db_balance": db_balance,
            "error": str(e),
            "dropped": False,
        }

    if not rep.get("ok"):
        return {
            "ok": False,
            "account_id": aid,
            "username": user,
            "status": status,
            "db_balance": db_balance,
            "error": str(rep.get("error") or "getBalance thất bại"),
            "dropped": False,
        }

    actual_raw = rep.get("balance")
    try:
        actual = float(actual_raw) if actual_raw is not None else 0.0
    except (TypeError, ValueError):
        actual = 0.0

    drop_amount = db_balance - actual
    min_notify = _min_drop_notify_vnd(cfg)
    # Chỉ báo khi lệch giảm > ngưỡng; lệch nhỏ (<= ngưỡng) bỏ qua.
    dropped = drop_amount > min_notify
    if actual < db_balance and not dropped:
        print(
            f"[BALANCE-RECONCILE] SKIP {user}: DB={_fmt_vnd(db_balance)} "
            f"→ thực tế={_fmt_vnd(actual)} "
            f"(lệch -{_fmt_vnd(drop_amount)} <= {_fmt_vnd(min_notify)})",
            flush=True,
        )
    elif dropped:
        delta = actual - db_balance
        msg = (
            f"User: {user} ({aid})\n"
            f"Status: {status or '(trống)'}\n"
            f"Số dư DB: {_fmt_vnd(db_balance)}\n"
            f"Số dư thực tế: {_fmt_vnd(actual)}\n"
            f"Chênh lệch: {_fmt_vnd(delta)}"
        )
        try:
            from xoso66_telegram_notify import notify_balance_drop

            notify_balance_drop(msg, cfg=cfg)
        except Exception as e:
            print(f"[BALANCE-RECONCILE] Telegram lỗi {user}: {e}", flush=True)
        print(
            f"[BALANCE-RECONCILE] DROP {user}: DB={_fmt_vnd(db_balance)} "
            f"→ thực tế={_fmt_vnd(actual)} ({_fmt_vnd(delta)})",
            flush=True,
        )

    return {
        "ok": True,
        "account_id": aid,
        "username": user,
        "status": status,
        "db_balance": db_balance,
        "balance": actual,
        "dropped": dropped,
    }


def run_balance_reconcile_once(*, cfg: dict | None = None) -> dict[str, Any]:
    """Một vòng check toàn bộ acc target."""
    c = cfg or load_config()
    rows = list_target_accounts()
    n = len(rows)
    workers = _parallel(c)
    print(
        f"[BALANCE-RECONCILE] Bắt đầu {n} acc "
        f"(Đang Chơi / Đủ ngày / Hết Tiền), {workers} luồng",
        flush=True,
    )
    if n == 0:
        return {"ok": True, "total": 0, "ok_count": 0, "fail_count": 0, "drop_count": 0}

    ok_n = fail_n = drop_n = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(_reconcile_one, str(r["id"]), cfg=c): str(r["id"])
            for r in rows
            if str(r.get("id") or "").strip()
        }
        for fut in as_completed(futs):
            if stopping():
                break
            try:
                rep = fut.result()
            except Exception as e:
                fail_n += 1
                print(f"[BALANCE-RECONCILE] Lỗi: {e}", flush=True)
                continue
            if rep.get("ok"):
                ok_n += 1
                if rep.get("dropped"):
                    drop_n += 1
            else:
                fail_n += 1
                print(
                    f"[BALANCE-RECONCILE] FAIL {rep.get('username')}: {rep.get('error')}",
                    flush=True,
                )

    print(
        f"[BALANCE-RECONCILE] Xong: {n} acc / ok={ok_n} / fail={fail_n} / drop={drop_n}",
        flush=True,
    )
    return {
        "ok": fail_n == 0,
        "total": n,
        "ok_count": ok_n,
        "fail_count": fail_n,
        "drop_count": drop_n,
    }


def worker_balance_reconcile_loop(*, quiet: bool = False) -> None:
    if not balance_reconcile_enabled():
        return
    interval_min = _interval_min()
    if not quiet:
        print(
            f"[BALANCE-RECONCILE] Worker mỗi {interval_min} phút — "
            f"Đang Chơi / Đủ ngày / Hết Tiền",
            flush=True,
        )
    while not stopping():
        try:
            run_balance_reconcile_once()
        except Exception as e:
            if not stopping():
                print(f"[BALANCE-RECONCILE] Lỗi vòng quét: {e}", flush=True)
        interval_min = _interval_min()
        sleep_sec = max(60, interval_min * 60)
        for _ in range(sleep_sec):
            if stopping():
                break
            time.sleep(1)
    print("[BALANCE-RECONCILE] Worker đã dừng.", flush=True)


def start_balance_reconcile_thread(*, quiet: bool = False) -> threading.Thread | None:
    if not balance_reconcile_enabled():
        return None
    t = threading.Thread(
        target=worker_balance_reconcile_loop,
        kwargs={"quiet": quiet},
        daemon=False,
        name="xoso66-balance-reconcile",
    )
    t.start()
    return t
