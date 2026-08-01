#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Đổi mật khẩu đăng nhập XOSO66 — POST /server/user/updatepassword (mã hóa).

Site chỉ chấp nhận 6-15 chữ cái + số (không ký tự đặc biệt).
  MotHaiBa4@  → bị từ chối
  MotHaiBa4   → OK

  python xoso66_change_login_password.py -u hainam19891 -n MotHaiBa4
  python xoso66_change_login_password.py --all -n MotHaiBa4 -j 8
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from xoso66_paths import apply_default_env

apply_default_env()

from xoso66_accounts_db import (  # noqa: E402
    get_account,
    get_account_by_username,
    init_db,
    list_accounts,
    list_accounts_by_status,
    update_account,
    username_for_log,
)
from xoso66_config_util import configure_stdio_utf8  # noqa: E402
from xoso66_session import ensure_session, persist_session, post_encrypted  # noqa: E402

configure_stdio_utf8()

UPDATE_LOGIN_PASSWORD_PATH = "/server/user/updatepassword"
_PRINT_LOCK = threading.Lock()


def validate_login_password(pwd: str) -> str:
    """Site: 6-15 chữ cái + số, không ký tự đặc biệt."""
    s = str(pwd or "").strip()
    if not (6 <= len(s) <= 15):
        raise ValueError("Mật khẩu phải 6-15 ký tự")
    if not s.isalnum():
        raise ValueError(
            "Site chỉ chấp nhận chữ cái + số (không ký tự đặc biệt như @). "
            f"Nhận: {pwd!r}"
        )
    return s


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


def _parse_api_body(data: Any) -> dict[str, Any]:
    if isinstance(data, dict) and "code" in data:
        return data
    if isinstance(data, dict) and data.get("_decrypt_error"):
        return data
    return {"_raw": data}


def change_login_password(
    session: dict,
    *,
    old_password: str,
    new_password: str,
) -> dict[str, Any]:
    """POST /server/user/updatepassword (encrypted)."""
    old = str(old_password or "").strip()
    new = validate_login_password(new_password)
    if not old:
        return {"ok": False, "error": "thiếu mật khẩu cũ"}
    if old == new:
        return {"ok": True, "skipped": True, "msg": "đã đúng mật khẩu mới", "code": 1}

    plain = {
        "old_password": old,
        "password": new,
        "confirm_password": new,
    }
    session.pop("aes_session_key", None)
    status, data, _ = post_encrypted(session, UPDATE_LOGIN_PASSWORD_PATH, plain)
    js = _parse_api_body(data)
    ok = status == 200 and js.get("code") == 1
    return {
        "ok": ok,
        "code": js.get("code"),
        "msg": js.get("msg"),
        "http_status": status,
        "error": None if ok else str(js.get("msg") or f"code={js.get('code')}"),
        "raw": js,
    }


def change_account_login_password(
    account_or_username: str,
    new_password: str,
    *,
    force_login: bool = False,
    skip_if_same: bool = True,
    verbose: bool = True,
    max_retries: int = 3,
) -> dict[str, Any]:
    aid = resolve_account_id(account_or_username)
    if not aid:
        return {"ok": False, "error": f"không tìm thấy account: {account_or_username!r}"}

    row = get_account(aid) or {}
    user = username_for_log(aid, row)
    old = str(row.get("password") or "").strip()
    try:
        new = validate_login_password(new_password)
    except ValueError as e:
        return {
            "ok": False,
            "account_id": aid,
            "username": user,
            "error": str(e),
        }

    if skip_if_same and old == new:
        if verbose:
            with _PRINT_LOCK:
                print(f"· [{user}] DB đã là mật khẩu mới — bỏ qua", flush=True)
        return {
            "ok": True,
            "skipped": True,
            "account_id": aid,
            "username": user,
            "db_updated": False,
            "msg": "db already new password",
        }

    if not old:
        return {
            "ok": False,
            "account_id": aid,
            "username": user,
            "error": "DB thiếu password cũ",
        }

    try:
        session = ensure_session(aid, force_login=force_login)
    except Exception as e:
        return {
            "ok": False,
            "account_id": aid,
            "username": user,
            "error": f"session: {e}",
        }

    # Session có thể còn password cũ trong JSON
    session["password"] = old

    last: dict[str, Any] = {}
    for attempt in range(1, max(1, max_retries) + 1):
        last = change_login_password(session, old_password=old, new_password=new)
        if last.get("ok"):
            break
        code = last.get("code")
        msg = str(last.get("error") or "")
        # rate limit / spam
        if code == 1049 or "thường xuyên" in msg.lower():
            time.sleep(2.0 * attempt)
            continue
        break

    if not last.get("ok"):
        if verbose:
            with _PRINT_LOCK:
                print(f"❌ [{user}] {last.get('error')}", flush=True)
        return {
            "ok": False,
            "account_id": aid,
            "username": user,
            "error": last.get("error"),
            "code": last.get("code"),
        }

    # Cập nhật DB + session
    update_account(
        aid,
        {
            "password": new,
            "session_json": {"password": new},
        },
    )
    session["password"] = new
    try:
        persist_session(aid, session)
    except Exception:
        pass

    if verbose:
        with _PRINT_LOCK:
            print(f"✅ [{user}] đổi MK OK → DB đã cập nhật", flush=True)
    return {
        "ok": True,
        "account_id": aid,
        "username": user,
        "db_updated": True,
        "msg": last.get("msg"),
        "skipped": bool(last.get("skipped")),
    }


def change_all_login_passwords(
    new_password: str,
    *,
    status_filter: str = "",
    workers: int = 8,
    force_login: bool = False,
    skip_if_same: bool = True,
    verbose: bool = True,
) -> dict[str, Any]:
    init_db()
    new = validate_login_password(new_password)
    st = str(status_filter or "").strip()
    if st:
        accounts = list_accounts_by_status(st)
    else:
        accounts = list_accounts()

    n_workers = max(1, min(32, int(workers or 8)))
    total = len(accounts)
    if verbose:
        print(
            f"🔑 Đổi MK đăng nhập → {new!r} — {total} acc — song song {n_workers}",
            flush=True,
        )

    ok_rows: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    def _one(row: dict[str, Any]) -> dict[str, Any]:
        return change_account_login_password(
            str(row.get("id") or ""),
            new,
            force_login=force_login,
            skip_if_same=skip_if_same,
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
            elif rep.get("skipped") and not rep.get("db_updated"):
                skipped.append(rep)
            else:
                ok_rows.append(rep)
            if verbose and (done % 10 == 0 or done == total):
                with _PRINT_LOCK:
                    print(
                        f"… {done}/{total} "
                        f"(ok={len(ok_rows)}, skip={len(skipped)}, lỗi={len(failed)})",
                        flush=True,
                    )

    ok_rows.sort(key=lambda r: str(r.get("username") or "").lower())
    failed.sort(key=lambda r: str(r.get("username") or "").lower())

    if verbose:
        print(
            f"\n=== Đổi MK: {len(ok_rows)} OK / {len(skipped)} bỏ qua / "
            f"{len(failed)} lỗi / {total} tổng ===",
            flush=True,
        )
        if failed:
            print("Lỗi:", flush=True)
            for r in failed:
                print(
                    f"  ✗ {r.get('username') or r.get('account_id')}: {r.get('error')}",
                    flush=True,
                )

    return {
        "ok": True,
        "new_password": new,
        "scanned": total,
        "changed": ok_rows,
        "skipped": skipped,
        "failed": failed,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Đổi mật khẩu đăng nhập XOSO66 → DB")
    ap.add_argument("-a", "--account", help="account id hoặc username")
    ap.add_argument("-u", "--username", help="username")
    ap.add_argument("--all", action="store_true", help="mọi account trong DB")
    ap.add_argument(
        "-n",
        "--new-password",
        default="MotHaiBa4",
        help="mật khẩu mới (mặc định MotHaiBa4; site không nhận ký tự đặc biệt)",
    )
    ap.add_argument("--status", default="", help="lọc status khi --all")
    ap.add_argument("-j", "--workers", type=int, default=8)
    ap.add_argument("--force-login", action="store_true")
    ap.add_argument(
        "--force-same",
        action="store_true",
        help="vẫn gọi API dù DB đã đúng mật khẩu mới",
    )
    args = ap.parse_args()

    init_db()
    try:
        new_pw = validate_login_password(args.new_password)
    except ValueError as e:
        print(f"❌ {e}", flush=True)
        print(
            "Gợi ý: dùng MotHaiBa4 (bỏ @) — site chỉ cho 6-15 chữ/số.",
            flush=True,
        )
        return 1

    if args.all:
        result = change_all_login_passwords(
            new_pw,
            status_filter=args.status,
            workers=int(args.workers),
            force_login=bool(args.force_login),
            skip_if_same=not bool(args.force_same),
            verbose=True,
        )
        return 0 if not (result.get("failed") or []) else 2

    key = (args.username or args.account or "").strip()
    if not key:
        ap.error("Cần -u/--username, -a hoặc --all")

    rep = change_account_login_password(
        key,
        new_pw,
        force_login=bool(args.force_login),
        skip_if_same=not bool(args.force_same),
        verbose=True,
    )
    return 0 if rep.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
