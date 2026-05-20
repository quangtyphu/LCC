# -*- coding: utf-8 -*-
"""
Đăng ký tài khoản XOSO66 — POST /server/user/register (mã hóa giống login).

Payload (từ bundle Vue):
  username, password, confirm_password, captcha, phone, email, truename,
  fund_password, sms_code, invite_code, fb_dynamic_pixel, fbclid, source, cid

Phụ trợ (không mã hóa):
  GET  /server/index/getcaptcha
  POST /server/index/smscode  { source: "register", phone }

Đăng ký thật: dùng Playwright (gọi Vue store — header CF theo URL, không cần captcha).
HTTP encrypt thuần hay trả code 1004 (cf-auth-token gắn URL).

CLI:
  python xoso66_register.py --random
  python xoso66_register.py -u Quang123 -p abc123456 --phone 0912345678 --truename "Ten A"
  python xoso66_register.py --random --save-acc acc4
"""

from __future__ import annotations

import argparse
import json
import random
import string
import sys
from typing import Any

import requests

from xoso66_session import (
    BASE_URL,
    bootstrap_prelogin,
    post_encrypted,
    refresh_cloudflare,
    _merge_response_cookies,
    _requests_session,
)
from xoso66_sessions_io import load_sessions, merge_account, save_sessions

REGISTER_PATH = "/server/user/register"
GET_CAPTCHA_PATH = "/server/index/getcaptcha"
SMS_CODE_PATH = "/server/index/smscode"

# Response đăng ký thành công (code=1 + data user đầy đủ — như browser sau POST register)
REGISTER_USER_REQUIRED_KEYS = ("username", "ukey", "status", "type", "register_time")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


def random_register_credentials() -> dict[str, str]:
    """Username [a-zA-Z][a-zA-Z0-9]{5,14}, password [a-zA-Z0-9]{6,15}, phone 10 số."""
    suffix = "".join(random.choices(string.digits, k=random.randint(3, 6)))
    username = f"Quang{suffix}"
    password = "abc" + "".join(random.choices(string.digits, k=6))
    phone = "09" + "".join(random.choices(string.digits, k=8))
    truename = random.choice(["Nguyen Van A", "Tran Thi B", "Le Van C", "Pham Van D"])
    return {"username": username, "password": password, "phone": phone, "truename": truename}


def new_guest_session(*, proxy: str = "", user_agent: str = "") -> dict:
    """Session trước đăng ký — proxy bắt buộc do caller truyền (không dùng default config)."""
    from xoso66_proxy import require_explicit_proxy

    s: dict[str, Any] = {
        "cookies": {"think_var": "vi-vn"},
        "headers": {},
        "user_agent": user_agent or DEFAULT_UA,
        "proxy": require_explicit_proxy(proxy),
    }
    return s


def seed_cf_from_account(guest: dict, account_id: str) -> None:
    """Copy cf_clearance + cf-* từ account có sẵn (tùy chọn, vẫn nên refresh)."""
    acc = load_sessions().get(account_id)
    if not acc:
        raise KeyError(f"Không có account '{account_id}'")
    cookies = dict(guest.get("cookies") or {})
    for k in ("cf_clearance", "__cf_bm", "PHPSESSID"):
        if acc.get("cookies", {}).get(k):
            cookies[k] = acc["cookies"][k]
    guest["cookies"] = cookies
    headers = dict(guest.get("headers") or {})
    for k in ("c-a-i", "cf-auth-token", "cf-con-s", "cf-pass"):
        v = (acc.get("headers") or {}).get(k)
        if v:
            headers[k] = v
    guest["headers"] = headers
    if acc.get("form_token"):
        guest["form_token"] = acc["form_token"]


def prepare_register_payload(
    username: str,
    password: str,
    *,
    confirm_password: str = "",
    captcha: str = "",
    phone: str = "",
    email: str = "",
    truename: str = "",
    fund_password: str = "",
    sms_code: str = "",
    invite_code: str = "",
    fb_dynamic_pixel: str = "",
    fbclid: str = "",
    source: str | None = None,
    cid: str = "",
    extra: dict | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "username": str(username).strip(),
        "password": str(password),
        "confirm_password": str(confirm_password or password),
        "captcha": str(captcha or ""),
        "phone": str(phone or ""),
        "email": str(email or ""),
        "truename": str(truename or ""),
        "fund_password": str(fund_password or ""),
        "sms_code": str(sms_code or ""),
        "invite_code": str(invite_code or ""),
        "fb_dynamic_pixel": str(fb_dynamic_pixel or ""),
        "fbclid": str(fbclid or ""),
        "cid": str(cid or ""),
    }
    if source:
        body["source"] = str(source)
    else:
        body["source"] = None
    if extra:
        body.update(extra)
    return body


def _plain_headers(session: dict, *, content_type: str = "application/json") -> dict[str, str]:
    from xoso66_deposit import build_common_headers, get_form_token

    return build_common_headers(session, form_token=get_form_token(session), content_type=content_type)


def get_captcha(session: dict) -> dict[str, Any]:
    """GET /server/index/getcaptcha."""
    from xoso66_deposit import apply_response_tokens

    http = _requests_session(session)
    r = http.get(
        f"{BASE_URL}{GET_CAPTCHA_PATH}",
        headers=_plain_headers(session, content_type="application/json"),
        timeout=25,
    )
    apply_response_tokens(session, r.headers)
    _merge_response_cookies(session, r)
    try:
        js = r.json()
    except Exception:
        return {"ok": False, "http_status": r.status_code, "raw": r.text[:500]}
    data = js.get("data") if isinstance(js.get("data"), dict) else {}
    cap = data.get("captcha") if isinstance(data.get("captcha"), dict) else {}
    inner = cap.get("data") if isinstance(cap.get("data"), dict) else cap
    url = str(inner.get("url") or inner.get("image") or cap.get("url") or "")
    return {"ok": js.get("code") == 1, "raw": js, "captcha_url": url}


def send_sms_code(session: dict, phone: str, *, source: str = "register") -> dict[str, Any]:
    """POST /server/index/smscode."""
    from xoso66_deposit import apply_response_tokens

    if not phone or len(phone) != 10 or not phone.isdigit():
        raise ValueError("phone phải là 10 chữ số (VD: 0912345678)")

    http = _requests_session(session)
    r = http.post(
        f"{BASE_URL}{SMS_CODE_PATH}",
        json={"source": source, "phone": phone},
        headers=_plain_headers(session),
        timeout=25,
    )
    apply_response_tokens(session, r.headers)
    _merge_response_cookies(session, r)
    try:
        js = r.json()
    except Exception:
        return {"ok": False, "http_status": r.status_code, "raw": r.text[:500]}
    return {
        "ok": js.get("code") == 1,
        "msg": js.get("msg"),
        "countdown": (js.get("data") or {}).get("countdown") if isinstance(js.get("data"), dict) else None,
        "raw": js,
    }


def register_failure_message(result: dict[str, Any]) -> str:
    """Rút msg ngắn từ response đăng ký (không dump captcha base64)."""
    if result.get("msg"):
        return str(result["msg"])
    code = result.get("code")
    hints = {
        1046: " — đổi username khác hoặc đăng nhập acc đã có",
    }
    hint = hints.get(int(code)) if code is not None else ""

    raw_err = result.get("error") or result.get("fail_reason") or ""
    if isinstance(raw_err, str):
        s = raw_err.strip()
        if s.startswith("Error:"):
            s = s[6:].strip()
        if s.startswith("{"):
            try:
                j = json.loads(s)
                msg = j.get("msg")
                c = j.get("code", code)
                if msg:
                    h = hints.get(int(c)) if c is not None else hint
                    return f"{msg} (code={c}){h or ''}"
            except json.JSONDecodeError:
                pass
    if raw_err:
        return str(raw_err)[:200]
    if code is not None:
        return f"Đăng ký thất bại (code={code}){hint}"
    return "Đăng ký thất bại"


def is_register_success_response(
    js: Any,
    *,
    expected_username: str = "",
) -> tuple[bool, str]:
    """
    Chỉ coi đăng ký thành công khi server trả profile user (giống JSON mẫu code=1).
    """
    if not isinstance(js, dict):
        return False, "response không phải JSON object"
    if js.get("code") != 1:
        return False, f"code={js.get('code')!r}"
    user = js.get("data")
    if not isinstance(user, dict):
        return False, "thiếu hoặc sai kiểu trường data"
    for key in REGISTER_USER_REQUIRED_KEYS:
        if key not in user:
            return False, f"thiếu data.{key}"
        if user[key] in (None, ""):
            return False, f"data.{key} rỗng"
    if int(user.get("status", 0)) != 1:
        return False, f"data.status={user.get('status')!r} (cần 1)"
    if int(user.get("type", 0)) != 1:
        return False, f"data.type={user.get('type')!r} (cần 1)"
    uname = str(user["username"]).strip()
    if expected_username and uname.lower() != str(expected_username).strip().lower():
        return False, f"username trả về '{uname}' khác '{expected_username}'"
    return True, ""


def ensure_guest_ready(session: dict, *, skip_cf: bool = False) -> dict[str, Any]:
    """Cloudflare + encryptKey + form-token."""
    report: dict[str, Any] = {"cf": None}
    if not skip_cf:
        cookies = session.get("cookies") or {}
        headers = session.get("headers") or {}
        need_cf = not cookies.get("cf_clearance") or not headers.get("cf-auth-token")
        if need_cf:
            report["cf"] = refresh_cloudflare(session)
            if not report["cf"].get("ok"):
                raise RuntimeError(f"Cloudflare thất bại: {report['cf']}")
    http = _requests_session(session)
    bootstrap_prelogin(session, http=http)
    report["form_token"] = bool(session.get("form_token"))
    report["cek_p"] = bool(session.get("cek_p"))
    return report


def register_account_playwright(
    plain: dict,
    *,
    headless: bool | None = None,
    proxy: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    """Đăng ký qua trình duyệt (Vue $store) — khuyến nghị."""
    import os

    try:
        from xoso66_playwright_ctx import playwright_browser
    except ImportError:
        raise RuntimeError("pip install playwright && playwright install chromium")

    from xoso66_cf import _apply_cookies

    if headless is None:
        headless = os.environ.get("XOSO66_CF_HEADLESS", "1") != "0"

    from xoso66_proxy import require_explicit_proxy

    proxy = require_explicit_proxy(proxy)
    session = new_guest_session(proxy=proxy, user_agent=user_agent)

    def _http_register_fallback(reason: str) -> dict[str, Any]:
        print(
            f"[REGISTER] {reason} → thử đăng ký HTTP (requests socks5h)…",
            flush=True,
        )
        return register_account(session, plain)

    pw_result: dict[str, Any] = {}
    tokens: dict[str, Any] = {}

    try:
        with playwright_browser(session, base_url=BASE_URL, headless=headless) as (
            _p,
            _browser,
            context,
        ):
            page = context.new_page()
            page.goto(f"{BASE_URL}/home/", wait_until="domcontentloaded", timeout=90_000)
            try:
                page.wait_for_load_state("networkidle", timeout=25_000)
            except Exception:
                page.wait_for_timeout(8_000)
            page.wait_for_timeout(2_000)

            pw_result = page.evaluate(
                """async (body) => {
                    const app = document.querySelector('#app');
                    const vm = app && app.__vue__;
                    if (!vm || !vm.$store) return { error: 'no_vue_store' };
                    try {
                        const r = await vm.$store.dispatch('user/register', body);
                        return { ok: true, response: r };
                    } catch (e) {
                        return { ok: false, error: String(e), message: e.message || '' };
                    }
                }""",
                plain,
            )

            tokens = page.evaluate(
                """() => {
                    const vm = document.querySelector('#app').__vue__;
                    const store = vm && vm.$store;
                    if (!store) return {};
                    return {
                        form_token: store.getters.fromToken || '',
                        cek_p: (store.state.app && store.state.app.cek_p) || ''
                    };
                }"""
            )
            _apply_cookies(session, context.cookies())
    except Exception as e:
        err = str(e)
        if "socks5 proxy authentication" in err.lower() or "socks5" in err.lower() and "auth" in err.lower():
            return _http_register_fallback("Playwright lỗi proxy SOCKS5")
        raise

    if pw_result.get("error"):
        err_body: dict[str, Any] = {"ok": False, "error": pw_result.get("error"), "session": session, "method": "playwright"}
        s = str(pw_result.get("error") or "").strip()
        if s.startswith("Error:"):
            s = s[6:].strip()
        if s.startswith("{"):
            try:
                j = json.loads(s)
                err_body["code"] = j.get("code")
                err_body["msg"] = j.get("msg")
            except json.JSONDecodeError:
                pass
        return err_body

    data = pw_result.get("response") if isinstance(pw_result.get("response"), dict) else {}
    user_data = data.get("data") if isinstance(data.get("data"), dict) else {}
    ok, fail_reason = is_register_success_response(data, expected_username=str(plain.get("username") or ""))
    if tokens.get("form_token"):
        session["form_token"] = tokens["form_token"]
    if ok:
        session["user_info"] = user_data
        session["username"] = user_data.get("username") or plain.get("username")
        session["password"] = plain.get("password")
        session["ukey"] = user_data.get("ukey")
    return {
        "ok": ok,
        "code": data.get("code"),
        "msg": data.get("msg"),
        "fail_reason": fail_reason or None,
        "data": user_data,
        "user_info": user_data if ok else None,
        "raw": data,
        "session": session,
        "method": "playwright",
    }


def register_account(
    session: dict,
    plain: dict,
    *,
    skip_cf: bool = False,
) -> dict[str, Any]:
    """
    POST /server/user/register (encrypted) — có thể 1004 nếu thiếu header CF theo URL.
    """
    ensure_guest_ready(session, skip_cf=skip_cf)
    session.pop("aes_session_key", None)
    http = _requests_session(session)
    status, data, _ = post_encrypted(session, REGISTER_PATH, plain, http=http)
    if status != 200:
        return {"ok": False, "http_status": status, "raw": data, "session": session}
    if not isinstance(data, dict):
        return {"ok": False, "error": "response không phải JSON", "raw": data, "session": session}

    user_data = data.get("data") if isinstance(data.get("data"), dict) else {}
    ok, fail_reason = is_register_success_response(
        data,
        expected_username=str(plain.get("username") or ""),
    )
    if ok:
        session["user_info"] = user_data
        session["username"] = user_data["username"]
        session["ukey"] = user_data.get("ukey")
    return {
        "ok": ok,
        "code": data.get("code"),
        "msg": data.get("msg"),
        "fail_reason": fail_reason or None,
        "data": user_data,
        "user_info": user_data if ok else None,
        "raw": data,
        "session": session,
        "method": "http",
    }


def register_account_auto(
    plain: dict,
    session: dict | None = None,
    *,
    proxy: str = "",
    prefer_playwright: bool = True,
    skip_cf: bool = False,
) -> dict[str, Any]:
    """Playwright trước; HTTP nếu prefer_playwright=False. proxy bắt buộc."""
    from xoso66_proxy import require_explicit_proxy

    px = require_explicit_proxy(proxy or (session or {}).get("proxy"))
    if prefer_playwright:
        return register_account_playwright(plain, proxy=px)
    guest = dict(session) if session else new_guest_session(proxy=px)
    guest["proxy"] = px
    return register_account(guest, plain, skip_cf=skip_cf)


def save_registered_account(account_id: str, username: str, password: str, session: dict) -> dict:
    accounts = load_sessions()
    entry = merge_account(
        {
            "id": account_id,
            "username": username,
            "password": password,
        },
        {
            "cookies": session.get("cookies"),
            "headers": session.get("headers"),
            "form_token": session.get("form_token"),
            "user_info": session.get("user_info"),
            "proxy": session.get("proxy"),
            "user_agent": session.get("user_agent"),
        },
    )
    accounts[account_id] = entry
    save_sessions(accounts)
    return entry


def main() -> int:
    parser = argparse.ArgumentParser(description="XOSO66 đăng ký tài khoản")
    parser.add_argument("-u", "--username", help="tên đăng nhập")
    parser.add_argument("-p", "--password", help="mật khẩu")
    parser.add_argument("--confirm", help="xác nhận mật khẩu (mặc định = password)")
    parser.add_argument("--phone", help="SĐT 10 số")
    parser.add_argument("--email", default="")
    parser.add_argument("--truename", default="", help="họ tên (nếu site bắt)")
    parser.add_argument("--fund-password", default="", help="mật khẩu rút tiền")
    parser.add_argument("--random", action="store_true", help="tự sinh user/pass/phone/tên")
    parser.add_argument("--http", action="store_true", help="đăng ký HTTP encrypt (dễ lỗi 1004)")
    parser.add_argument("--captcha", default="", help="captcha (site thường không bắt)")
    parser.add_argument("--sms-code", default="", help="mã SMS")
    parser.add_argument("--invite", default="", help="mã mời")
    parser.add_argument("--cid", default="")
    parser.add_argument(
        "--proxy",
        required=True,
        help='BẮT BUỘC — SOCKS5 host:port:user:pass (vd. 118.70.171.104:20023:user:pass)',
    )
    parser.add_argument("--cf-from", metavar="ACC", help="copy cf từ account trong sessions rồi refresh")
    parser.add_argument("--get-captcha", action="store_true", help="lấy captcha, in JSON")
    parser.add_argument("--sms", action="store_true", help="gửi SMS (cần --phone)")
    parser.add_argument("--skip-cf", action="store_true", help="bỏ refresh CF (đã có cookie hợp lệ)")
    parser.add_argument("--save-acc", metavar="ID", help="lưu vào xoso66_sessions.json")
    parser.add_argument("--dry", action="store_true", help="chỉ bootstrap CF + form-token")
    args = parser.parse_args()

    try:
        guest = new_guest_session(proxy=args.proxy)
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1
    if args.cf_from:
        seed_cf_from_account(guest, args.cf_from)

    try:
        if args.dry:
            rep = ensure_guest_ready(guest, skip_cf=args.skip_cf)
            print(
                json.dumps(
                    {"ok": True, "bootstrap": rep, "form_token": guest.get("form_token")},
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0

        if args.get_captcha:
            ensure_guest_ready(guest, skip_cf=args.skip_cf)
            out = get_captcha(guest)
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return 0 if out.get("ok") else 1

        if args.sms:
            if not args.phone:
                print("Cần --phone cho --sms", file=sys.stderr)
                return 1
            ensure_guest_ready(guest, skip_cf=args.skip_cf)
            out = send_sms_code(guest, args.phone)
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return 0 if out.get("ok") else 1

        creds = random_register_credentials() if args.random else {}
        username = args.username or creds.get("username")
        password = args.password or creds.get("password")
        phone = args.phone or creds.get("phone") or ""
        truename = args.truename or creds.get("truename") or ""

        if not username or not password:
            parser.print_help()
            return 1
        if not phone or len(phone) != 10 or not phone.isdigit():
            print("Cần --phone 10 chữ số (hoặc --random)", file=sys.stderr)
            return 1
        if not truename:
            truename = "Nguyen Van A"

        plain = prepare_register_payload(
            username,
            password,
            confirm_password=args.confirm or password,
            captcha=args.captcha,
            phone=phone,
            email=args.email,
            truename=truename,
            fund_password=args.fund_password,
            sms_code=args.sms_code,
            invite_code=args.invite,
            cid=args.cid,
        )
        if args.http:
            result = register_account(guest, plain, skip_cf=args.skip_cf)
            sess = guest
        else:
            result = register_account_playwright(plain, proxy=args.proxy)
            sess = result.get("session") or guest

        login_name = (result.get("user_info") or {}).get("username") or username
        payload = {
            "username": username,
            "login_username": login_name,
            "password": password,
            "phone": phone,
            "truename": truename,
            "method": result.get("method"),
            "ok": result.get("ok"),
            "code": result.get("code"),
            "msg": result.get("msg"),
            "fail_reason": result.get("fail_reason"),
            "user_info": result.get("user_info"),
        }
        if args.save_acc and result.get("ok"):
            save_registered_account(args.save_acc, login_name, password, sess)
            payload["saved_account_id"] = args.save_acc
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if result.get("ok") else 1
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, indent=2, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
