# -*- coding: utf-8 -*-
"""
Login XOSO66 qua Google Chrome CMS profile.

Mặc định: mở Google Chrome (không CDP) → tự điền + bấm «Đăng nhập» (UI Automation)
→ sync cookie. Không dùng Chrome for Testing (trang trắng với profile CMS).

  python xoso66_chrome_manual_login.py
  python xoso66_chrome_manual_login.py dareyoubo
  python xoso66_chrome_manual_login.py -a acc17918
  python xoso66_chrome_manual_login.py dareyoubo --manual   # chỉ mở Chrome, login tay
  python xoso66_chrome_manual_login.py dareyoubo --manual --no-wait
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

from xoso66_paths import apply_default_env

apply_default_env()

from xoso66_accounts_db import (  # noqa: E402
    STATUS_DANG_CHOI,
    get_account,
    get_account_by_username,
    set_account_status,
    update_account,
    username_for_log,
)
from xoso66_chrome_profile import (  # noqa: E402
    launch_cms_chrome,
    warm_session_from_profile,
)
from xoso66_cms_chrome import resolve_cms_chrome_by_device  # noqa: E402
from xoso66_game_domain import default_base_url  # noqa: E402
from xoso66_session import (  # noqa: E402
    bootstrap_prelogin,
    get_user_balance,
    persist_session,
    refresh_account_balance_to_db,
)
from xoso66_sessions_io import load_sessions  # noqa: E402


def _resolve_account(username: str = "", account_id: str = "") -> dict:
    if account_id.strip():
        row = get_account(account_id.strip())
        if not row:
            raise KeyError(f"Không có account_id {account_id!r}")
        return row
    u = username.strip()
    if not u:
        raise ValueError("Cần username hoặc -a account_id")
    row = get_account_by_username(u)
    if not row:
        raise KeyError(f"Không tìm thấy username {u!r}")
    return row


def open_cms_chrome_like_manual(account_id: str) -> dict:
    """
    Mở Chrome y hệt CMS (cdp_port=0) — không gắn Playwright.
    Trả {ok, proc, profile_dir, device, proxy, start_url}.
    """
    row = get_account(account_id) or {}
    aid = str(row.get("id") or account_id).strip()
    dev = str(row.get("device") or "").strip()
    if not dev:
        raise ValueError(f"{aid}: thiếu device CMS (vd. 1PG16)")

    cms = resolve_cms_chrome_by_device(dev)
    if not cms:
        raise ValueError(f"Không tìm thấy Chrome CMS device {dev!r}")

    profile_dir = Path(str(cms.get("profile_dir") or ""))
    if not profile_dir.is_dir():
        raise ValueError(f"Profile không tồn tại: {profile_dir}")

    proxy = str(cms.get("proxy") or row.get("proxy") or "").strip()
    if not proxy:
        raise ValueError(f"{dev}: thiếu proxy")

    update_account(aid, {"proxy": proxy})
    sess = load_sessions().get(aid) or {"id": aid}
    sess["id"] = aid
    sess["proxy"] = proxy
    persist_session(aid, sess)

    start_url = f"{default_base_url()}/home/"
    proc = launch_cms_chrome(profile_dir, proxy, urls=[start_url], cdp_port=0)
    user = username_for_log(aid, row)
    print(
        f"[CHROME] {user} ({dev}) — mở Chrome tay (không CDP)\n"
        f"  profile: {profile_dir}\n"
        f"  proxy:   {proxy.split(':')[0]}:{proxy.split(':')[1] if ':' in proxy else ''}\n"
        f"  url:     {start_url}\n"
        f"  → Đăng nhập trên cửa sổ Chrome, rồi ĐÓNG Chrome để sync session.",
        flush=True,
    )
    return {
        "ok": True,
        "proc": proc,
        "profile_dir": profile_dir,
        "device": dev,
        "proxy": proxy,
        "start_url": start_url,
        "account_id": aid,
    }


def wait_chrome_closed(proc, *, poll_sec: float = 0.5) -> None:
    while proc.poll() is None:
        time.sleep(poll_sec)


def sync_after_manual_login(account_id: str, profile_dir: Path) -> dict:
    """Đọc cookie profile sau khi đóng Chrome → getBalance → Đang Chơi (retry nếu phiên chưa ăn)."""
    from urllib.parse import urlparse

    from xoso66_chrome_profile import read_profile_cookies_after_close, warm_session_from_profile
    from xoso66_game_domain import resolve_base_url
    from xoso66_session import strip_identity_cookies

    aid = str(account_id).strip()
    sess = load_sessions()[aid]
    sess.setdefault("id", aid)
    base = resolve_base_url(sess) or default_base_url()
    sess["base_url"] = base
    host = urlparse(base).netloc

    last_bal: dict = {}
    has_sess = False
    for attempt in range(1, 4):
        # Không giữ PHPSESSID cũ từ JSON — chỉ tin cookie đọc từ Chrome.
        strip_identity_cookies(sess)
        loaded = read_profile_cookies_after_close(
            sess, profile_dir, host=host, allow_kill=False
        )
        has_sess = bool((sess.get("cookies") or {}).get("PHPSESSID"))
        php = str((sess.get("cookies") or {}).get("PHPSESSID") or "")
        print(
            f"[SYNC] try={attempt} cookies={sorted((sess.get('cookies') or {}).keys())} "
            f"PHPSESSID={has_sess} php={php[:12] + '…' if php else '-'} "
            f"restarted={loaded.get('restarted_chrome')}",
            flush=True,
        )
        if not has_sess:
            warm_session_from_profile(
                sess, profile_dir, host=host, allow_identity=True
            )
            has_sess = bool((sess.get("cookies") or {}).get("PHPSESSID"))
        if not has_sess:
            print(
                f"[SYNC] Profile Chrome không có PHPSESSID (try {attempt}/3) — "
                "login UI có thể chưa thành công hoặc cookie chưa flush",
                flush=True,
            )
            time.sleep(1.2 + attempt * 0.4)
            continue

        bootstrap_prelogin(sess)
        bal = get_user_balance(sess, refresh=True)
        last_bal = bal if isinstance(bal, dict) else {}
        if last_bal.get("ok"):
            persist_session(aid, sess)
            refresh_account_balance_to_db(aid, sess, refresh=True)
            row = get_account(aid) or {}
            if str(row.get("status") or "").strip() != STATUS_DANG_CHOI:
                set_account_status(
                    aid, STATUS_DANG_CHOI, reason="Sync Chrome sau login (không CDP)"
                )
            return {
                "ok": True,
                "balance": last_bal.get("balance"),
                "status": get_account(aid).get("status"),
                "has_phpsessid": True,
                "sync_tries": attempt,
            }

        raw = last_bal.get("raw") if isinstance(last_bal.get("raw"), dict) else {}
        msg = str(raw.get("msg") or last_bal.get("reason") or "session chưa hợp lệ")
        print(f"[SYNC] getBalance chưa OK (try {attempt}/3): {msg}", flush=True)
        time.sleep(1.2 + attempt * 0.4)
        warm_session_from_profile(sess, profile_dir, host=host, allow_identity=True)

    raw = last_bal.get("raw") if isinstance(last_bal.get("raw"), dict) else {}
    return {
        "ok": False,
        "error": "getBalance_fail" if has_sess else "missing_phpsessid_in_profile",
        "msg": str(
            raw.get("msg")
            or last_bal.get("reason")
            or (
                "Chrome profile không có PHPSESSID sau login"
                if not has_sess
                else "session chưa hợp lệ"
            )
        ),
        "code": raw.get("code"),
        "has_phpsessid": has_sess,
        "sync_tries": 3,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Login XOSO66: auto extension (mặc định) hoặc --manual"
    )
    ap.add_argument("username", nargs="?", default="", help="username site")
    ap.add_argument("-a", "--account", default="", help="account id (acc…)")
    ap.add_argument(
        "--manual",
        action="store_true",
        help="Chỉ mở Chrome CMS (không CDP), login tay rồi đóng để sync",
    )
    ap.add_argument(
        "--no-wait",
        action="store_true",
        help="Với --manual: chỉ mở Chrome, không chờ đóng / không sync",
    )
    ap.add_argument(
        "--ready-only",
        action="store_true",
        help="Chỉ test mở Chrome tới form Đăng nhập sẵn sàng (không login)",
    )
    ap.add_argument("--timeout", type=int, default=180, help="Timeout auto login (giây)")
    args = ap.parse_args()
    username = str(args.username or "").strip()
    account_id = str(args.account or "").strip()
    if not username and not account_id:
        try:
            username = input("Username cần login: ").strip()
        except EOFError:
            username = ""
        if not username:
            print("Lỗi: chưa nhập username", flush=True)
            return 1
    try:
        row = _resolve_account(username, account_id)
    except Exception as e:
        print(f"Lỗi: {e}", flush=True)
        return 1

    aid = str(row["id"])

    if args.ready_only:
        from xoso66_chrome_ext_login import wait_chrome_login_form_ready
        from xoso66_chrome_profile import mark_chrome_clean_exit, terminate_chrome_profile
        from xoso66_game_domain import resolve_base_url

        dev = str(row.get("device") or "").strip()
        cms = resolve_cms_chrome_by_device(dev) if dev else None
        if not cms:
            print(f"[READY] Không có Chrome CMS device {dev!r}", flush=True)
            return 1
        profile_dir = Path(str(cms.get("profile_dir") or ""))
        proxy = str(cms.get("proxy") or row.get("proxy") or "").strip()
        if not profile_dir.is_dir() or not proxy:
            print("[READY] Thiếu profile/proxy", flush=True)
            return 1
        terminate_chrome_profile(profile_dir)
        time.sleep(0.8)
        mark_chrome_clean_exit(profile_dir)
        sess = load_sessions().get(aid) or {}
        base = resolve_base_url(sess) or default_base_url()
        url = f"{base}/home/"
        print(f"[READY] user={username_for_log(aid)} device={dev} url={url}", flush=True)
        out = wait_chrome_login_form_ready(
            profile_dir=profile_dir,
            proxy=proxy,
            url=url,
            timeout_sec=int(args.timeout),
        )
        if out.get("ok"):
            print(
                f"[READY-OK] {username_for_log(aid)} elapsed={out.get('elapsed_sec')}s "
                f"F5={out.get('refresh_count')} title={out.get('title')!r}",
                flush=True,
            )
            return 0
        print(f"[READY-FAIL] {out}", flush=True)
        return 1

    if not args.manual:
        from xoso66_chrome_ext_login import login_account_via_extension

        out = login_account_via_extension(aid, timeout_sec=int(args.timeout))
        if out.get("ok"):
            print(
                f"[OK] {username_for_log(aid)} balance={out.get('balance')} "
                f"status={out.get('status')} method={out.get('method')}",
                flush=True,
            )
            return 0
        print(f"[FAIL] {out.get('msg') or out.get('error') or out}", flush=True)
        return 1

    try:
        opened = open_cms_chrome_like_manual(aid)
    except Exception as e:
        print(f"[CHROME] Mở fail: {e}", flush=True)
        return 1

    if args.no_wait:
        print("[CHROME] --no-wait: Chrome đang chạy, tự sync sau khi login+đóng.", flush=True)
        return 0

    proc = opened["proc"]
    print("[CHROME] Đang chờ bạn đóng cửa sổ Chrome…", flush=True)
    wait_chrome_closed(proc)
    time.sleep(1.0)
    print("[CHROME] Đã đóng — sync cookie…", flush=True)
    out = sync_after_manual_login(aid, Path(opened["profile_dir"]))
    if out.get("ok"):
        print(
            f"[OK] {username_for_log(aid)} balance={out.get('balance')} "
            f"status={out.get('status')}",
            flush=True,
        )
        return 0
    print(f"[FAIL] {out.get('msg') or out}", flush=True)
    print(
        "Gợi ý: login đúng cửa sổ vừa mở, đợi vào trang đã login rồi mới đóng Chrome.",
        flush=True,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
