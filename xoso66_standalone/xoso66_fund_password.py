# -*- coding: utf-8 -*-
"""
Đặt / đổi mật khẩu rút tiền (6 số) — POST /server/user/updatefundpassword (mã hóa).

Không phải mật khẩu đăng nhập. Dùng khi rút tiền (thay OTP).

Payload (từ Vue chageTransactionPwd):
  old_fund_password: "" nếu chưa có MK rút; MK cũ 6 số nếu đổi
  fund_password: MK rút mới (6 số)
  confirm_fund_password: nhập lại MK mới

Body gửi lên là chuỗi base64 AES (vd. vbqeyAmnuC3GTxl7...) + header cek-k.

CLI:
  python xoso66_fund_password.py -a acc1 -p 123456
  python xoso66_fund_password.py -a acc1 -p 123456 --old 111111
  python xoso66_fund_password.py -a acc1 -p 123456 --check
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from xoso66_session import (
    BASE_URL,
    ensure_session,
    merge_playwright_cookies,
    post_encrypted,
    _merge_response_cookies,
    _requests_session,
)
from xoso66_sessions_io import load_sessions, save_sessions

UPDATE_FUND_PASSWORD_PATH = "/server/user/updatefundpassword"
USER_INFO_PATH = "/server/user/info"


def validate_fund_password(pwd: str) -> str:
    s = str(pwd).strip()
    if len(s) != 6 or not s.isdigit():
        raise ValueError("Mật khẩu rút tiền phải đúng 6 chữ số")
    return s


def prepare_update_fund_password_payload(
    fund_password: str,
    *,
    confirm_fund_password: str = "",
    old_fund_password: str = "",
) -> dict[str, str]:
    pwd = validate_fund_password(fund_password)
    confirm = validate_fund_password(confirm_fund_password or fund_password)
    if pwd != confirm:
        raise ValueError("fund_password và confirm_fund_password không khớp")
    old = str(old_fund_password or "").strip()
    if old and (len(old) != 6 or not old.isdigit()):
        raise ValueError("old_fund_password phải rỗng hoặc đúng 6 chữ số")
    return {
        "old_fund_password": old,
        "fund_password": pwd,
        "confirm_fund_password": confirm,
    }


def _parse_api_body(data: Any, raw_text: str = "") -> dict[str, Any]:
    if isinstance(data, dict) and "code" in data:
        return data
    if isinstance(data, dict) and data.get("_decrypt_error"):
        preview = str(data.get("_cipher_preview") or raw_text or "")
        preview = preview.strip()
        if preview.startswith('"') and preview.endswith('"'):
            preview = preview[1:-1]
        try:
            return json.loads(preview)
        except Exception:
            pass
    if raw_text.strip().startswith("{"):
        try:
            return json.loads(raw_text)
        except Exception:
            pass
    return {"_raw": data}


def get_user_info(session: dict, *, is_user_center: bool = True) -> dict[str, Any]:
    from xoso66_deposit import apply_response_tokens, build_common_headers, get_form_token

    headers = build_common_headers(
        session,
        form_token=get_form_token(session),
        content_type="application/x-www-form-urlencoded/json",
    )
    body = {"isUserCenter": True} if is_user_center else {}
    r = _requests_session(session).post(
        f"{BASE_URL}{USER_INFO_PATH}",
        json=body,
        headers=headers,
        timeout=25,
    )
    apply_response_tokens(session, r.headers)
    _merge_response_cookies(session, r)
    try:
        js = r.json()
    except Exception:
        return {"ok": False, "http_status": r.status_code, "raw": r.text[:400]}
    data = js.get("data") if isinstance(js.get("data"), dict) else {}
    return {
        "ok": js.get("code") == 1,
        "code": js.get("code"),
        "msg": js.get("msg"),
        "isset_fundpassword": data.get("isset_fundpassword"),
        "data": data,
        "raw": js,
    }


def is_fund_password_set_response(js: dict[str, Any]) -> tuple[bool, str]:
    if js.get("code") != 1:
        return False, str(js.get("msg") or f"code={js.get('code')}")
    return True, ""


def update_fund_password(
    session: dict,
    plain: dict,
) -> dict[str, Any]:
    """POST updatefundpassword (encrypted). Cần session đã login."""
    session.pop("aes_session_key", None)
    http = _requests_session(session)
    status, data, _ = post_encrypted(session, UPDATE_FUND_PASSWORD_PATH, plain, http=http)
    js = _parse_api_body(data)
    ok, reason = is_fund_password_set_response(js)
    return {
        "ok": ok,
        "code": js.get("code"),
        "msg": js.get("msg"),
        "fail_reason": reason or None,
        "raw": js,
        "http_status": status,
        "session": session,
    }


def update_fund_password_playwright(
    session: dict,
    plain: dict,
) -> dict[str, Any]:
    try:
        from xoso66_playwright_ctx import playwright_browser
    except ImportError:
        raise RuntimeError("pip install playwright && playwright install chromium")

    extra = {
        "accept": "application/json",
        "x-lang": "vi",
        "x-device": "pc",
    }
    for k in ("c-a-i", "cf-auth-token", "cf-con-s", "cf-pass"):
        v = (session.get("headers") or {}).get(k)
        if v:
            extra[k] = v
    if session.get("form_token"):
        extra["form-token"] = session["form_token"]

    with playwright_browser(session, base_url=BASE_URL, headless=True, extra_http_headers=extra) as (
        _p,
        _browser,
        context,
    ):
        page = context.new_page()
        page.goto(f"{BASE_URL}/home/", wait_until="domcontentloaded", timeout=90_000)
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            page.wait_for_timeout(6_000)
        page.wait_for_timeout(2_000)
        js = page.evaluate(
            """async (body) => {
                const vm = document.querySelector('#app').__vue__;
                return await vm.$store.dispatch('userCenter/updatefundpassword', body);
            }""",
            plain,
        )
        merge_playwright_cookies(session, context.cookies())

    if not isinstance(js, dict):
        return {"ok": False, "error": "response không hợp lệ", "raw": js, "session": session}
    ok, reason = is_fund_password_set_response(js)
    return {
        "ok": ok,
        "code": js.get("code"),
        "msg": js.get("msg"),
        "fail_reason": reason or None,
        "raw": js,
        "session": session,
        "method": "playwright",
    }


def set_fund_password_for_account(
    account_id: str,
    fund_password: str,
    *,
    old_fund_password: str = "",
    use_playwright: bool = False,
    verify: bool = True,
) -> dict[str, Any]:
    from xoso66_proxy import proxy_has_auth, resolve_proxy

    session = ensure_session(account_id)
    plain = prepare_update_fund_password_payload(
        fund_password,
        old_fund_password=old_fund_password,
    )
    if use_playwright and proxy_has_auth(resolve_proxy(session)):
        print("[FUND_PW] Proxy có user/pass → HTTP, không Playwright", flush=True)
        use_playwright = False
    if use_playwright:
        result = update_fund_password_playwright(session, plain)
    else:
        result = update_fund_password(session, plain)
        if (
            not result.get("ok")
            and result.get("code") in (1004, 10058, None)
            and not proxy_has_auth(resolve_proxy(session))
        ):
            result = update_fund_password_playwright(session, plain)

    if verify and result.get("ok"):
        info = get_user_info(session)
        result["isset_fundpassword"] = info.get("isset_fundpassword")
        result["verify_ok"] = info.get("isset_fundpassword") in (1, "1", True)
        if not result["verify_ok"]:
            result["ok"] = False
            result["fail_reason"] = "API code=1 nhưng user/info chưa isset_fundpassword=1"

    accounts = load_sessions()
    accounts[account_id] = session
    save_sessions(accounts)
    result["account_id"] = account_id
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="XOSO66 mật khẩu rút tiền (6 số)")
    parser.add_argument("-a", "--account", required=True, help="id trong xoso66_sessions.json")
    parser.add_argument("-p", "--password", help="MK rút mới (6 số)")
    parser.add_argument("--confirm", help="nhập lại (mặc định = --password)")
    parser.add_argument("--old", default="", help="MK rút cũ (khi đổi)")
    parser.add_argument("--browser", action="store_true", help="ép Playwright")
    parser.add_argument("--check", action="store_true", help="chỉ xem isset_fundpassword")
    args = parser.parse_args()

    try:
        if args.check:
            session = ensure_session(args.account)
            info = get_user_info(session)
            accounts = load_sessions()
            accounts[args.account] = session
            save_sessions(accounts)
            print(
                json.dumps(
                    {
                        "account_id": args.account,
                        "isset_fundpassword": info.get("isset_fundpassword"),
                        **info,
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0

        if not args.password:
            parser.print_help()
            return 1

        result = set_fund_password_for_account(
            args.account,
            args.password,
            old_fund_password=args.old,
            use_playwright=args.browser,
        )
        print(
            json.dumps(
                {
                    "account_id": args.account,
                    "ok": result.get("ok"),
                    "code": result.get("code"),
                    "msg": result.get("msg"),
                    "fail_reason": result.get("fail_reason"),
                    "isset_fundpassword": result.get("isset_fundpassword"),
                    "method": result.get("method", "http"),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0 if result.get("ok") else 1
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, indent=2, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
