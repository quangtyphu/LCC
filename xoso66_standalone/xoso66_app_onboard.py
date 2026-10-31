#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Flow APP (giống Bet66 native — KHÔNG Playwright / Chrome):

  1. Đăng ký     POST /server/user/register           (cipher + header android_app)
  2. MK rút      POST /server/user/updatefundpassword  (cipher)
  3. Liên kết NH POST /server/user/bankcardbind        (cipher)

Header APP (mitm com.bet66.naapp):
  x-device=android, x-app-platform="", x-app-version=8,
  x-app-build-version=991, x-requested-with=com.bet66.naapp

Ví dụ:
  python xoso66_app_onboard.py --demo
  python xoso66_app_onboard.py -u user01 -p Pass1234 --phone 0912345678 \\
      --holder \"NGUYEN VAN A\" --stk 0123456789 --bank VPB --fund-password 270288 \\
      --proxy host:port:user:pass
"""
from __future__ import annotations

import argparse
import json
import random
import string
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _step(name: str, ok: bool, **extra: Any) -> dict[str, Any]:
    return {"step": name, "ok": bool(ok), **extra}


def _rand_username(n: int = 10) -> str:
    first = random.choice(string.ascii_lowercase)
    rest = "".join(random.choices(string.ascii_lowercase + string.digits, k=n - 1))
    return first + rest


def _rand_password() -> str:
    chars = (
        random.choice(string.ascii_uppercase)
        + random.choice(string.ascii_lowercase)
        + random.choice(string.digits)
        + "".join(random.choices(string.ascii_letters + string.digits, k=5))
    )
    lst = list(chars)
    random.shuffle(lst)
    return "".join(lst)


def _rand_phone() -> str:
    return "09" + "".join(random.choices(string.digits, k=8))


def _rand_holder() -> str:
    last = random.choice(["NGUYEN", "TRAN", "LE", "PHAM", "HOANG", "VU", "DO", "BUI"])
    mid = random.choice(["VAN", "THI", "MINH", "DUC", "QUANG", "HUY"])
    first = random.choice(["AN", "BINH", "CUONG", "DUNG", "HUNG", "LONG", "NAM", "TUAN"])
    return f"{last} {mid} {first}"


def _rand_stk() -> str:
    return "".join(random.choices(string.digits, k=random.choice([10, 12, 13, 14])))


def _rand_bank() -> str:
    return random.choice(["VPB", "MB", "TCB", "ACB", "VCB", "BIDV"])


def _proxy_key(proxy: str) -> str:
    from xoso66_proxy import parse_proxy

    try:
        host, port, user, pwd = parse_proxy(proxy)
        return f"{host.lower()}:{port}:{user}:{pwd}"
    except Exception:
        return str(proxy or "").strip().lower()


def _borrow_cf_from_same_proxy(proxy: str) -> dict[str, Any] | None:
    """Lấy cf_clearance / cf-auth-token từ TK cùng proxy (tránh mở browser)."""
    from xoso66_accounts_db import list_accounts
    from xoso66_sessions_io import load_sessions

    want = _proxy_key(proxy)
    sessions = load_sessions()
    for acc in list_accounts():
        if _proxy_key(str(acc.get("proxy") or "")) != want:
            continue
        aid = str(acc.get("id") or "")
        sess = sessions.get(aid) or {}
        cookies = sess.get("cookies") or {}
        headers = sess.get("headers") or {}
        if cookies.get("cf_clearance") or headers.get("cf-auth-token"):
            return {
                "cookies": dict(cookies),
                "headers": dict(headers),
                "form_token": sess.get("form_token"),
                "cek_p": sess.get("cek_p"),
                "user_agent": sess.get("user_agent"),
                "from_account": aid,
            }
    return None


def _warm_guest_app(proxy: str) -> dict[str, Any]:
    """Session guest profile APP + CF (ưu tiên mượn cookie; không Playwright)."""
    from xoso66_cf import refresh_cf_curl_cffi, session_cf_ready
    from xoso66_red_packet import apply_client_profile
    from xoso66_register import ensure_guest_ready, new_guest_session

    guest = new_guest_session(proxy=proxy)
    apply_client_profile(guest, "android_app")
    guest["proxy"] = proxy

    borrowed = _borrow_cf_from_same_proxy(proxy)
    if borrowed:
        cookies = dict(guest.get("cookies") or {})
        cookies.update(borrowed.get("cookies") or {})
        guest["cookies"] = cookies
        hdr = dict(guest.get("headers") or {})
        hdr.update({k: v for k, v in (borrowed.get("headers") or {}).items() if v})
        guest["headers"] = hdr
        if borrowed.get("form_token"):
            guest["form_token"] = borrowed["form_token"]
        if borrowed.get("cek_p"):
            guest["cek_p"] = borrowed["cek_p"]
        print(
            f"[APP_ONBOARD] CF mượn từ {borrowed.get('from_account')} (cùng proxy)",
            flush=True,
        )

    if not session_cf_ready(guest):
        # Chỉ curl_cffi — KHÔNG gọi refresh_cloudflare (sẽ ép Playwright khi proxy có auth)
        print("[APP_ONBOARD] CF qua curl_cffi (không Playwright)…", flush=True)
        cf = refresh_cf_curl_cffi(guest)
        if not session_cf_ready(guest):
            raise RuntimeError(f"Cloudflare chưa sẵn (không dùng Playwright): {cf}")

    ensure_guest_ready(guest, skip_cf=True)
    if not guest.get("cek_p"):
        raise RuntimeError("Thiếu cek_p sau encryptKey — kiểm tra proxy/CF")
    return guest


def run_onboard(
    *,
    username: str,
    password: str,
    phone: str,
    account_holder: str,
    fund_password: str,
    account_number: str,
    bank: str,
    proxy: str,
) -> dict[str, Any]:
    from xoso66_accounts_db import (
        create_account,
        get_account,
        get_account_by_username,
        save_session_runtime,
        update_account,
        append_provision_log,
    )
    from xoso66_banks import normalize_bank
    from xoso66_bank_bind import bind_bank_for_account
    from xoso66_fund_password import set_fund_password_for_account
    from xoso66_register import (
        prepare_register_payload,
        register_account_http_with_captcha,
        register_failure_message,
    )
    from xoso66_session import login_account, merge_account

    steps: list[dict[str, Any]] = []
    print(
        "[APP_ONBOARD] Flow APP HTTP: register → updatefundpassword → bankcardbind",
        flush=True,
    )
    print(
        f"[APP_ONBOARD] user={username} fund_pw={fund_password} "
        f"bank={bank} stk={account_number} holder={account_holder}",
        flush=True,
    )

    existing = get_account_by_username(username)
    if existing:
        st = _step(
            "register",
            True,
            skipped=True,
            msg=f"username đã có DB ({existing.get('id')})",
        )
        steps.append(st)
        return {
            "ok": False,
            "account_id": existing.get("id"),
            "steps": steps,
            "msg": "username trùng — chọn tên khác",
        }

    # --- 1) Register ---
    print(f"[APP_ONBOARD] Đăng ký {username} (android_app, HTTP)…", flush=True)
    try:
        guest = _warm_guest_app(proxy)
        plain = prepare_register_payload(
            username, password, phone=phone, truename=account_holder
        )
        reg = register_account_http_with_captcha(
            plain, proxy=proxy, session=guest, skip_cf=True
        )
    except Exception as e:
        st = _step("register", False, msg=str(e))
        steps.append(st)
        return {"ok": False, "steps": steps}

    sess = reg.get("session") or guest
    if not reg.get("ok"):
        msg = register_failure_message(reg)
        if reg.get("http_status") == 475:
            msg = (
                "Cloudflare HTTP 475 chặn POST /server/user/register từ PC "
                "(APP trên máy thật qua được; curl_cffi không đủ — không dùng Playwright)."
            )
        st = _step(
            "register",
            False,
            msg=msg,
            code=reg.get("code"),
            http_status=reg.get("http_status"),
        )
        steps.append(st)
        return {"ok": False, "steps": steps, "raw": reg.get("raw"), "http_status": reg.get("http_status")}

    try:
        login_patch = login_account(sess)
        merge_account(sess, login_patch)
    except Exception as e:
        st = _step("register", False, msg=f"login sau đăng ký: {e}")
        steps.append(st)
        return {"ok": False, "steps": steps, "session": sess}

    try:
        row = create_account(
            {
                "username": username,
                "password": password,
                "phone": phone,
                "account_holder": account_holder,
                "proxy": proxy,
                "device": "",
                "fund_password": "",
                "status": "registered",
                "session_json": sess,
            }
        )
        account_id = row["id"]
        save_session_runtime(account_id, sess)
    except Exception as e:
        st = _step("register", False, msg=f"lưu DB: {e}")
        steps.append(st)
        return {"ok": False, "steps": steps, "session": sess}

    st = _step("register", True, msg=reg.get("msg") or "Đăng ký thành công")
    steps.append(st)
    append_provision_log(account_id, st)

    # --- 2) Fund password ---
    print(f"[APP_ONBOARD] Đặt MK rút {account_id}…", flush=True)
    try:
        fp = set_fund_password_for_account(
            account_id, fund_password, use_playwright=False
        )
        if fp.get("ok"):
            update_account(
                account_id,
                {"fund_password": fund_password, "status": "fund_password_ok"},
            )
            st = _step("fund_password", True, msg=fp.get("msg") or "Đặt MK rút OK")
        else:
            st = _step(
                "fund_password",
                False,
                msg=fp.get("msg") or fp.get("fail_reason") or "Đặt MK rút thất bại",
            )
    except Exception as e:
        st = _step("fund_password", False, msg=str(e))
    steps.append(st)
    append_provision_log(account_id, st)
    if not st["ok"]:
        return {
            "ok": False,
            "account_id": account_id,
            "steps": steps,
            "account": get_account(account_id),
        }

    # --- 3) Bank bind ---
    bank_code, bank_name = normalize_bank(bank)
    print(f"[APP_ONBOARD] Liên kết bank {bank_name} {account_number}…", flush=True)
    try:
        bk = bind_bank_for_account(
            account_id,
            bank_name=bank_name or bank,
            cardnumber=account_number,
            truename=account_holder,
            fund_password=fund_password,
            branch="",
            use_playwright=False,
        )
        if bk.get("ok"):
            cards = bk.get("linked_cards") or []
            card_id = None
            for card in cards:
                if isinstance(card, dict) and card.get("card_id"):
                    card_id = card["card_id"]
                    break
            patch: dict[str, Any] = {
                "account_number": account_number,
                "bank_code": bank_code,
                "bank_name": bank_name or bank,
                "status": "bank_linked",
            }
            if card_id:
                patch["default_card_id"] = card_id
            if cards:
                patch["linked_banks"] = cards
            update_account(account_id, patch)
            if bk.get("session"):
                save_session_runtime(account_id, bk["session"])
            st = _step("bank_bind", True, msg="Liên kết bank thành công", default_card_id=card_id)
        else:
            st = _step(
                "bank_bind",
                False,
                msg=bk.get("msg") or bk.get("fail_reason") or "Liên kết bank thất bại",
            )
    except Exception as e:
        st = _step("bank_bind", False, msg=str(e))
    steps.append(st)
    append_provision_log(account_id, st)

    return {
        "ok": all(s.get("ok") for s in steps),
        "account_id": account_id,
        "steps": steps,
        "account": get_account(account_id),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="XOSO66 APP onboard HTTP (register → MK rút → bind bank) — không Playwright"
    )
    ap.add_argument("--demo", action="store_true", help="tạo TK random + bind bank random")
    ap.add_argument("-u", "--username", default="")
    ap.add_argument("-p", "--password", default="")
    ap.add_argument("--phone", default="")
    ap.add_argument("--holder", default="", help="họ tên chủ TK / truename")
    ap.add_argument("--fund-password", default="270288", help="mật khẩu rút tiền (mặc định 270288)")
    ap.add_argument("--stk", default="", help="số tài khoản ngân hàng")
    ap.add_argument("--bank", default="", help="mã NH: VPB, MB, TCB, …")
    ap.add_argument("--proxy", default="", help="proxy host:port:user:pass")
    args = ap.parse_args()

    if args.demo:
        username = args.username or _rand_username()
        password = args.password or _rand_password()
        phone = args.phone or _rand_phone()
        holder = args.holder or _rand_holder()
        stk = args.stk or _rand_stk()
        bank = args.bank or _rand_bank()
    else:
        username = args.username
        password = args.password
        phone = args.phone
        holder = args.holder
        stk = args.stk
        bank = args.bank
        if not all([username, password, phone, holder, stk, bank]):
            ap.error("Thiếu tham số — dùng --demo hoặc truyền đủ -u/-p/--phone/--holder/--stk/--bank")

    fund_password = args.fund_password or "270288"
    proxy = args.proxy
    if not proxy:
        # Ưu tiên proxy đang có session CF (TK Đang Chơi)
        from xoso66_accounts_db import list_accounts

        playing = [a for a in list_accounts() if a.get("status") == "Đang Chơi" and a.get("proxy")]
        proxy = str((playing[0] if playing else {}).get("proxy") or "")
    if not proxy:
        from xoso66_config_util import hardcoded_default_proxy

        proxy = hardcoded_default_proxy()
    if not proxy:
        print("[APP_ONBOARD] Cần --proxy", flush=True)
        return 2

    result = run_onboard(
        username=username,
        password=password,
        phone=phone,
        account_holder=holder,
        fund_password=fund_password,
        account_number=stk,
        bank=bank,
        proxy=proxy,
    )

    print("\n=== KẾT QUẢ FLOW APP ===", flush=True)
    for st in result.get("steps") or []:
        flag = "OK" if st.get("ok") else "FAIL"
        skip = " (skip)" if st.get("skipped") else ""
        print(f"  [{flag}]{skip} {st.get('step')}: {st.get('msg')}", flush=True)
    print(f"  account_id={result.get('account_id')}", flush=True)
    print(f"  overall_ok={result.get('ok')}", flush=True)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str), flush=True)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
