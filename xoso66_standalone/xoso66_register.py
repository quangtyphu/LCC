# -*- coding: utf-8 -*-
"""
Đăng ký tài khoản XOSO66 — POST /server/user/register (mã hóa giống login).

Payload (từ bundle Vue):
  username, password, confirm_password, captcha, phone, email, truename,
  fund_password, sms_code, invite_code, fb_dynamic_pixel, fbclid, source, cid

Phụ trợ (không mã hóa):
  GET  /server/index/getcaptcha
  POST /server/index/smscode  { source: "register", phone }

Đăng ký thật: dùng Playwright (gọi Vue store) + Capsolver ImageToText khi site bắt captcha ảnh.
HTTP encrypt thuần hay trả code 1004 (cf-auth-token gắn URL).

CLI:
  python xoso66_register.py --random
  python xoso66_register.py -u Quang123 -p abc123456 --phone 0912345678 --truename "Ten A"
  python xoso66_register.py --random --save-acc acc4
"""

from __future__ import annotations

import argparse
import json
import os
import random
import string
import sys
from pathlib import Path
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
    """Copy chỉ CF cookies/headers từ account có sẵn — không copy PHPSESSID/form_token."""
    acc = load_sessions().get(account_id)
    if not acc:
        raise KeyError(f"Không có account '{account_id}'")
    cookies = dict(guest.get("cookies") or {})
    # Chỉ CF — PHPSESSID/form_token của nick khác làm lẫn số dư (vd. ak47 ↔ consoverhnz).
    for k in ("cf_clearance", "__cf_bm"):
        if acc.get("cookies", {}).get(k):
            cookies[k] = acc["cookies"][k]
    guest["cookies"] = cookies
    headers = dict(guest.get("headers") or {})
    for k in ("c-a-i", "cf-auth-token", "cf-con-s", "cf-pass"):
        v = (acc.get("headers") or {}).get(k)
        if v:
            headers[k] = v
    guest["headers"] = headers


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
    if result.get("error") in ("no_vue_store", "cf_verify_blocked"):
        return str(result.get("msg") or "Cloudflare/Vue chưa sẵn sàng — thử proxy khác")
    if result.get("msg"):
        return str(result["msg"])
    code = result.get("code")
    hints = {
        1046: " — đổi username khác hoặc đăng nhập acc đã có",
        1049: " — chờ 5–10 phút rồi thử lại (IP/proxy bị giới hạn tần suất)",
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


_REGISTER_RATE_LIMIT_CODE = 1049


def is_register_rate_limit_code(code: int | None) -> bool:
    return code == _REGISTER_RATE_LIMIT_CODE


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


def _register_via_vue_browser(
    session: dict,
    plain: dict,
    *,
    cf_meta: dict[str, Any] | None = None,
    method: str = "cms_chrome",
    browser_mode: str = "cms_persistent",
) -> dict[str, Any]:
    """Đăng ký qua Vue store trong browser."""
    import os

    from xoso66_cf import _apply_cookies, bootstrap_register_page, _inject_session_cookies
    from xoso66_captcha_solver import (
        EXTRACT_STORE_TOKENS_JS,
        REGISTER_DISPATCH_JS,
        captcha_enabled,
        is_wrong_captcha_code,
        load_captcha_config,
        parse_register_error,
        solve_image_captcha_auto,
        solve_register_captcha_from_page,
    )
    from xoso66_playwright_ctx import (
        playwright_browser,
        playwright_cms_profile_browser,
        playwright_register_browser,
    )

    headless = os.environ.get("XOSO66_CF_HEADLESS", "0") != "0"
    cap_cfg = load_captcha_config()
    max_attempts = max(1, int(cap_cfg.get("max_attempts") or 3))
    meta = dict(cf_meta or {})
    captcha_meta: dict[str, Any] = {}
    pw_result: dict[str, Any] = {}
    tokens: dict[str, Any] = {}

    mode = (browser_mode or "cms_persistent").strip().lower()
    use_native = mode == "native_cdp" and os.environ.get(
        "XOSO66_REGISTER_NATIVE_CHROME", "1"
    ).strip().lower() not in ("0", "false", "no")
    use_cms_persistent = mode == "cms_persistent"
    label = {
        "native_cdp": "Chrome native + CDP",
        "cms_persistent": "Playwright persistent profile CMS",
        "ephemeral": "Chrome tạm + cookie CMS",
    }.get(mode, mode)

    def _run_page(context: Any) -> dict[str, Any]:
        nonlocal pw_result, tokens, captcha_meta
        page = context.pages[0] if context.pages else context.new_page()
        if mode == "ephemeral":
            from xoso66_cf import SITE_HOST

            _inject_session_cookies(context, session, SITE_HOST)
        print(f"[REGISTER] Vue dispatch ({label})…", flush=True)
        has_cf = bool((session.get("cookies") or {}).get("cf_clearance"))
        boot = bootstrap_register_page(
            page,
            session,
            context=context,
            headless=headless,
            native_launch=use_native or (use_cms_persistent and has_cf),
        )
        if isinstance(boot.get("meta"), dict):
            meta.update(boot["meta"])
        if not boot.get("ok"):
            return {
                "ok": False,
                "error": boot.get("error") or "bootstrap_failed",
                "msg": boot.get("msg"),
                "cf_meta": meta,
                "method": method,
                "session": session,
            }

        # Không gửi captcha trước — site thường không bắt (chỉ khi sai nhiều lần → code 1011).
        for attempt in range(max_attempts):
            pw_result = page.evaluate(REGISTER_DISPATCH_JS, plain)
            resp = pw_result.get("response") if isinstance(pw_result.get("response"), dict) else {}
            api_code = resp.get("code") if resp else pw_result.get("code")
            api_msg = resp.get("msg") if resp else pw_result.get("msg")
            if api_code is not None or api_msg:
                print(
                    f"[REGISTER] API lần {attempt + 1}: code={api_code} msg={api_msg or ''}",
                    flush=True,
                )
            if pw_result.get("ok"):
                break
            err_text = str(pw_result.get("message") or pw_result.get("error") or "")
            code, msg, cap_b64 = parse_register_error(err_text)
            if pw_result.get("error") == "no_vue_store":
                break
            if not is_wrong_captcha_code(code) or attempt + 1 >= max_attempts:
                if code is not None:
                    pw_result.setdefault("code", code)
                if msg:
                    pw_result.setdefault("msg", msg)
                break
            if not captcha_enabled():
                break
            solved = (
                solve_image_captcha_auto(cap_b64)
                if cap_b64
                else solve_register_captcha_from_page(page)
            )
            captcha_meta[f"retry_{attempt + 1}"] = {"ok": solved.get("ok"), "code": code}
            if not solved.get("ok"):
                pw_result["captcha_error"] = solved.get("error")
                break
            plain["captcha"] = str(solved.get("text") or "").strip()
            print(f"[REGISTER] Captcha retry {attempt + 1}: {plain['captcha']!r}", flush=True)

        if pw_result.get("ok"):
            tokens = page.evaluate(EXTRACT_STORE_TOKENS_JS)
        _apply_cookies(session, context.cookies())
        return {}

    try:
        if use_native:
            with playwright_register_browser(session, headless=headless, channel="chrome") as (
                _p,
                context,
            ):
                err = _run_page(context)
                if err:
                    return err
        elif use_cms_persistent:
            with playwright_cms_profile_browser(session, headless=headless, channel="chrome") as (
                _p,
                context,
            ):
                err = _run_page(context)
                if err:
                    return err
        else:
            with playwright_browser(
                session,
                base_url=BASE_URL,
                headless=headless,
                channel="chrome",
                ignore_automation=True,
            ) as (_p, _browser, context):
                err = _run_page(context)
                if err:
                    return err
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "cf_meta": meta,
            "method": method,
            "session": session,
        }

    if pw_result.get("error"):
        err_body: dict[str, Any] = {
            "ok": False,
            "error": pw_result.get("error"),
            "msg": pw_result.get("msg"),
            "cf_meta": meta,
            "captcha": captcha_meta or None,
            "method": method,
            "session": session,
        }
        err_text = str(pw_result.get("message") or pw_result.get("error") or "")
        if '"status":475' in err_text or '"status": 475' in err_text:
            err_body["http_status"] = 475
            err_body["msg"] = (
                "Cloudflare chặn POST /user/register (HTTP 475). "
                "Cookie CMS đã OK nhưng CF vẫn chặn khi automation gửi đăng ký — "
                "thử đóng Chrome CMS (profile XMSB17) rồi chạy lại, hoặc đăng ký tay trong Chrome CMS."
            )
        code, msg, _ = parse_register_error(err_text)
        if msg and msg != str(pw_result.get("error") or ""):
            err_body["msg"] = msg
        if code is not None:
            err_body["code"] = code
        return err_body

    data = pw_result.get("response") if isinstance(pw_result.get("response"), dict) else {}
    user_data = data.get("data") if isinstance(data.get("data"), dict) else {}
    ok, fail_reason = is_register_success_response(
        data,
        expected_username=str(plain.get("username") or ""),
    )
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
        "cf_meta": meta,
        "captcha": captcha_meta or None,
        "method": method,
        "session": session,
    }


def _prefetch_register_captcha(session: dict, plain: dict) -> dict[str, Any]:
    """Lấy + giải captcha qua HTTP, ghi vào plain."""
    from xoso66_captcha_solver import (
        captcha_base64_from_payload,
        captcha_enabled,
        solve_image_captcha_auto,
    )

    meta: dict[str, Any] = {}
    if not captcha_enabled() or str(plain.get("captcha") or "").strip():
        return meta
    try:
        ensure_guest_ready(session, skip_cf=True)
        cap = get_captcha(session)
        b64 = captcha_base64_from_payload(cap.get("raw") or {})
        if b64:
            solved = solve_image_captcha_auto(b64)
            meta["prefetch"] = {"ok": solved.get("ok"), "error": solved.get("error")}
            if solved.get("ok"):
                plain["captcha"] = str(solved.get("text") or "").strip()
                print(f"[REGISTER] Captcha HTTP: {plain['captcha']!r}", flush=True)
    except Exception as e:
        meta["prefetch"] = {"ok": False, "error": str(e)}
    return meta


def _resolve_cms_register_context(
    *,
    cms_device: str = "",
    proxy: str = "",
) -> tuple[str, dict[str, Any] | None]:
    """Gắn XOSO66_CMS_DEVICE + kiểm tra profile CMS tồn tại."""
    import os

    from xoso66_cms_chrome import resolve_cms_chrome_by_device

    dev = str(cms_device or os.environ.get("XOSO66_CMS_DEVICE") or "").strip()
    if not dev:
        return proxy, None
    os.environ["XOSO66_CMS_DEVICE"] = dev
    row = resolve_cms_chrome_by_device(dev)
    if not row or not row.get("profile_dir"):
        raise ValueError(f"Không tìm thấy Chrome profile CMS: {dev}")
    pdir = Path(str(row["profile_dir"]))
    if not pdir.is_dir():
        raise ValueError(f"Thư mục profile CMS không tồn tại ({dev}): {pdir}")
    cms_proxy = str(row.get("proxy") or "").strip()
    use_proxy = str(proxy or cms_proxy or "").strip()
    if not use_proxy:
        raise ValueError(f"Profile CMS {dev} thiếu proxy")
    print(f"[PROVISION] CMS device {dev} → {pdir}", flush=True)
    return use_proxy, row


def register_account_via_cms_chrome(
    plain: dict,
    *,
    proxy: str = "",
    user_agent: str = "",
    cms_device: str = "",
) -> dict[str, Any]:
    """
    Đăng ký tự động:
      1) Chrome CMS + extension (không CDP)
      2) Playwright persistent profile CMS
    """
    import os
    import time

    from xoso66_chrome_ext_register import register_via_chrome_extension
    from xoso66_chrome_profile import (
        cms_chrome_warm_session,
        profile_is_locked,
        wait_profile_unlocked,
    )
    from xoso66_playwright_ctx import _register_profile_dir
    from xoso66_proxy import require_explicit_proxy

    dev = str(cms_device or os.environ.get("XOSO66_CMS_DEVICE") or "").strip()
    if dev:
        os.environ["XOSO66_CMS_DEVICE"] = dev
    proxy = require_explicit_proxy(proxy)
    session = new_guest_session(proxy=proxy, user_agent=user_agent)
    profile_dir = _register_profile_dir(proxy)
    cf_meta = cms_chrome_warm_session(session, profile_dir, cms_device=dev)

    if not cf_meta.get("ok") and not cf_meta.get("has_clearance"):
        who = dev or profile_dir.name
        return {
            "ok": False,
            "error": cf_meta.get("error") or "cf_warm_failed",
            "msg": (
                f"Cloudflare chưa cho qua (cf_clearance) — profile {who}. "
                "Mở Chrome CMS, giải captcha chọn con vật nếu có, rồi bấm Đăng ký lại."
            ),
            "cf_meta": cf_meta,
            "method": "cms_chrome",
            "session": session,
        }

    captcha_meta: dict[str, Any] = {}
    if os.environ.get("XOSO66_REGISTER_PREFETCH_CAPTCHA", "0").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        captcha_meta = _prefetch_register_captcha(session, plain)

    if profile_is_locked(profile_dir):
        print(
            "[REGISTER] Chrome đang giữ profile CMS — chờ đóng (tối đa 30s)…",
            flush=True,
        )
        if not wait_profile_unlocked(profile_dir, timeout_sec=30):
            return {
                "ok": False,
                "error": "profile_in_use",
                "msg": (
                    f"Profile {dev or profile_dir.name} đang mở trong Chrome. "
                    "Đóng cửa sổ Chrome đó rồi bấm Đăng ký lại."
                ),
                "cf_meta": cf_meta,
                "method": "cms_chrome",
                "session": session,
            }

    # 1) Playwright persistent — đã chứng minh gọi được API register
    print("[REGISTER] Playwright persistent profile CMS…", flush=True)
    from xoso66_chrome_profile import warm_session_from_profile

    warm_session_from_profile(session, profile_dir)
    max_rate_retries = max(1, int(os.environ.get("XOSO66_REGISTER_RATE_RETRY", "3")))
    result: dict[str, Any] = {}

    for rate_try in range(max_rate_retries):
        if rate_try > 0:
            wait_s = 45 * rate_try
            print(f"[REGISTER] Rate limit — chờ {wait_s}s rồi thử lại…", flush=True)
            time.sleep(wait_s)
            plain.pop("captcha", None)

        result = _register_via_vue_browser(
            session,
            plain,
            cf_meta=cf_meta,
            method="cms_chrome",
            browser_mode="cms_persistent",
        )
        result["captcha"] = captcha_meta or result.get("captcha")
        if result.get("ok"):
            return result
        if is_register_rate_limit_code(result.get("code")):
            continue
        if result.get("http_status") == 475 or '"status":475' in str(result.get("error") or ""):
            print("[REGISTER] 475 — refresh cookie profile…", flush=True)
            cms_chrome_warm_session(session, profile_dir, cms_device=dev)
            warm_session_from_profile(session, profile_dir)
            result = _register_via_vue_browser(
                session,
                plain,
                cf_meta=cf_meta,
                method="cms_chrome",
                browser_mode="cms_persistent",
            )
            result["captcha"] = captcha_meta or result.get("captcha")
            if result.get("ok"):
                return result
        break

    if result.get("ok"):
        return result

    # 2) Extension (tùy chọn) — Chrome thật, không CDP
    use_ext = os.environ.get("XOSO66_REGISTER_USE_EXT", "0").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if not use_ext:
        return result

    if profile_is_locked(profile_dir) and not wait_profile_unlocked(profile_dir, timeout_sec=15):
        result.setdefault(
            "msg",
            result.get("msg") or "Playwright thất bại — profile vẫn đang mở, không chạy extension.",
        )
        return result

    print("[REGISTER] Chrome extension (fallback)…", flush=True)
    ext_out = register_via_chrome_extension(session, plain, profile_dir, timeout_sec=90)
    cf_meta["extension"] = {k: v for k, v in ext_out.items() if k not in ("response", "result")}
    if ext_out.get("ok"):
        data = ext_out.get("response") if isinstance(ext_out.get("response"), dict) else {}
        if not data and isinstance(ext_out.get("result"), dict):
            data = ext_out["result"].get("response") if isinstance(ext_out["result"].get("response"), dict) else {}
        user_data = data.get("data") if isinstance(data.get("data"), dict) else {}
        ok, fail_reason = is_register_success_response(
            data, expected_username=str(plain.get("username") or "")
        )
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
            "user_info": user_data if ok else None,
            "raw": data,
            "cf_meta": cf_meta,
            "captcha": captcha_meta or None,
            "method": "cms_chrome_ext",
            "session": session,
        }

    ext_code = None
    ext_msg = str(ext_out.get("error") or ext_out.get("message") or "")
    if ext_out.get("response") and isinstance(ext_out["response"], dict):
        ext_code = ext_out["response"].get("code")
    if is_register_rate_limit_code(ext_code):
        for rate_try in range(1, max(1, int(os.environ.get("XOSO66_REGISTER_RATE_RETRY", "3")))):
            wait_s = 45 * rate_try
            print(f"[REGISTER] Rate limit {ext_code} — chờ {wait_s}s…", flush=True)
            time.sleep(wait_s)
            plain.pop("captcha", None)
            captcha_meta.update(_prefetch_register_captcha(session, plain))
            ext_out = register_via_chrome_extension(session, plain, profile_dir, timeout_sec=150)
            if ext_out.get("ok"):
                data = ext_out.get("response") if isinstance(ext_out.get("response"), dict) else {}
                user_data = data.get("data") if isinstance(data.get("data"), dict) else {}
                ok, fail_reason = is_register_success_response(
                    data, expected_username=str(plain.get("username") or "")
                )
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
                    "user_info": user_data if ok else None,
                    "raw": data,
                    "cf_meta": cf_meta,
                    "captcha": captcha_meta or None,
                    "method": "cms_chrome_ext",
                    "session": session,
                }

    if ext_msg and not result.get("msg"):
        result["msg"] = ext_msg
    result["cf_meta"] = cf_meta
    return result


def register_account_playwright(
    plain: dict,
    *,
    headless: bool | None = None,
    proxy: str = "",
    user_agent: str = "",
    cms_device: str = "",
) -> dict[str, Any]:
    """Đăng ký — mặc định CMS Chrome (không CDP); mode=cdp mới dùng Playwright."""
    import os

    mode = os.environ.get("XOSO66_REGISTER_MODE", "cms").strip().lower()
    if mode != "cdp":
        out = register_account_via_cms_chrome(
            plain,
            proxy=proxy,
            user_agent=user_agent,
            cms_device=cms_device,
        )
        if out.get("ok"):
            return out
        if os.environ.get("XOSO66_REGISTER_USE_CDP", "0").strip().lower() not in (
            "1",
            "true",
            "yes",
        ):
            return out
        print("[REGISTER] CMS HTTP thất bại → fallback CDP (dễ bị CF chọn con vật)…", flush=True)

    try:
        from xoso66_playwright_ctx import playwright_register_browser
    except ImportError:
        raise RuntimeError("pip install playwright && playwright install chromium")

    from xoso66_cf import _apply_cookies, bootstrap_register_page
    from xoso66_captcha_solver import (
        EXTRACT_STORE_TOKENS_JS,
        REGISTER_DISPATCH_JS,
        captcha_enabled,
        is_wrong_captcha_code,
        load_captcha_config,
        parse_register_error,
        solve_image_captcha_auto,
        solve_register_captcha_from_page,
    )
    from xoso66_proxy import require_explicit_proxy

    if headless is None:
        reg_hl = os.environ.get("XOSO66_REGISTER_HEADLESS")
        if reg_hl is not None:
            headless = reg_hl.strip() not in ("0", "false", "no")
        else:
            headless = os.environ.get("XOSO66_CF_HEADLESS", "0") != "0"
    pw_channel = (os.environ.get("XOSO66_PW_CHANNEL") or "chrome").strip() or None

    proxy = require_explicit_proxy(proxy)
    session = new_guest_session(proxy=proxy, user_agent=user_agent)
    cap_cfg = load_captcha_config()
    max_attempts = max(1, int(cap_cfg.get("max_attempts") or 3))
    cf_meta: dict[str, Any] = {}

    def _http_register_fallback(reason: str) -> dict[str, Any]:
        print(
            f"[REGISTER] {reason} → thử đăng ký HTTP (requests socks5h)…",
            flush=True,
        )
        try:
            ensure_guest_ready(session, skip_cf=True)
        except Exception as e:
            return {
                "ok": False,
                "error": "http_bootstrap_failed",
                "msg": str(e),
                "session": session,
                "method": "http",
            }
        return register_account(session, plain, skip_cf=True)

    def _should_http_fallback(pw_result: dict[str, Any]) -> bool:
        if pw_result.get("ok"):
            return False
        err = str(pw_result.get("error") or pw_result.get("message") or "")
        if pw_result.get("error") in ("no_vue_store", "cf_verify_blocked"):
            return False
        return bool(err)

    pw_result: dict[str, Any] = {}
    tokens: dict[str, Any] = {}
    captcha_meta: dict[str, Any] = {}

    try:
        with playwright_register_browser(
            session,
            headless=headless,
            channel=pw_channel,
        ) as (
            _p,
            context,
        ):
            page = context.pages[0] if context.pages else context.new_page()
            boot = bootstrap_register_page(
                page,
                session,
                context=context,
                headless=headless,
                native_launch=os.environ.get("XOSO66_REGISTER_NATIVE_CHROME", "1").strip().lower()
                not in ("0", "false", "no"),
            )
            cf_meta = boot.get("meta") if isinstance(boot.get("meta"), dict) else {}
            if not boot.get("ok"):
                pw_result = {
                    "error": boot.get("error") or "no_vue_store",
                    "msg": boot.get("msg"),
                }
            elif captcha_enabled() and not str(plain.get("captcha") or "").strip():
                solved = solve_register_captcha_from_page(page)
                captcha_meta["prefetch"] = {
                    "ok": solved.get("ok"),
                    "text_len": len(str(solved.get("text") or "")),
                }
                if solved.get("ok"):
                    plain["captcha"] = str(solved.get("text") or "").strip()
                    print(f"[REGISTER] Captcha prefetch: {plain['captcha']!r}", flush=True)
                else:
                    print(
                        f"[REGISTER] Captcha prefetch thất bại: {solved.get('error')}",
                        flush=True,
                    )

            if not pw_result.get("error"):
                for attempt in range(max_attempts):
                    pw_result = page.evaluate(REGISTER_DISPATCH_JS, plain)
                    if pw_result.get("ok"):
                        break

                    err_text = str(pw_result.get("message") or pw_result.get("error") or "")
                    code, msg, cap_b64 = parse_register_error(err_text)
                    if pw_result.get("error") == "no_vue_store":
                        break
                    if not is_wrong_captcha_code(code) or attempt + 1 >= max_attempts:
                        if code is not None:
                            pw_result.setdefault("code", code)
                        if msg:
                            pw_result.setdefault("msg", msg)
                        break
                    if not captcha_enabled():
                        break

                    solved: dict[str, Any]
                    if cap_b64:
                        solved = solve_image_captcha_auto(cap_b64)
                    else:
                        solved = solve_register_captcha_from_page(page)
                    captcha_meta[f"retry_{attempt + 1}"] = {
                        "ok": solved.get("ok"),
                        "code": code,
                    }
                    if not solved.get("ok"):
                        pw_result["captcha_error"] = solved.get("error")
                        break
                    plain["captcha"] = str(solved.get("text") or "").strip()
                    print(
                        f"[REGISTER] Captcha retry {attempt + 1}: {plain['captcha']!r}",
                        flush=True,
                    )

            if pw_result.get("ok"):
                tokens = page.evaluate(EXTRACT_STORE_TOKENS_JS)
            else:
                tokens = {}
            _apply_cookies(session, context.cookies())
    except Exception as e:
        err = str(e)
        if "socks5 proxy authentication" in err.lower() or "socks5" in err.lower() and "auth" in err.lower():
            return _http_register_fallback("Playwright lỗi proxy SOCKS5")
        raise

    if pw_result.get("error"):
        if _should_http_fallback(pw_result):
            http_res = _http_register_fallback("Vue/API lỗi")
            http_res["cf_meta"] = cf_meta or None
            http_res["captcha"] = captcha_meta or None
            if http_res.get("ok"):
                return http_res
        err_body: dict[str, Any] = {
            "ok": False,
            "error": pw_result.get("error"),
            "session": session,
            "method": "playwright",
            "captcha": captcha_meta or None,
            "cf_meta": cf_meta or None,
        }
        err_text = str(pw_result.get("message") or pw_result.get("error") or "")
        code, msg, _ = parse_register_error(err_text)
        if pw_result.get("msg"):
            err_body["msg"] = pw_result.get("msg")
        elif msg and msg != str(pw_result.get("error") or ""):
            err_body["msg"] = msg
        if code is not None:
            err_body["code"] = code
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
        "captcha": captcha_meta or None,
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
    """Playwright trước; HTTP nếu prefer_playwright=False. Proxy bắt buộc."""
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
        "--cms-device",
        default=os.environ.get("XOSO66_CMS_DEVICE", ""),
        help="Thiết bị CMS (vd. XMSB17) — dùng chrome_profiles_data + proxy từ game_data.db",
    )
    parser.add_argument(
        "--proxy",
        default="",
        help='SOCKS5 host:port:user:pass — bỏ qua nếu có --cms-device (lấy proxy từ CMS)',
    )
    parser.add_argument("--cf-from", metavar="ACC", help="copy cf từ account trong sessions rồi refresh")
    parser.add_argument("--get-captcha", action="store_true", help="lấy captcha, in JSON")
    parser.add_argument("--sms", action="store_true", help="gửi SMS (cần --phone)")
    parser.add_argument("--skip-cf", action="store_true", help="bỏ refresh CF (đã có cookie hợp lệ)")
    parser.add_argument("--save-acc", metavar="ID", help="lưu vào xoso66_sessions.json")
    parser.add_argument("--dry", action="store_true", help="chỉ bootstrap CF + form-token")
    args = parser.parse_args()

    cms_device = str(args.cms_device or os.environ.get("XOSO66_CMS_DEVICE") or "").strip()
    proxy = str(args.proxy or "").strip()
    if cms_device:
        os.environ["XOSO66_CMS_DEVICE"] = cms_device
        if not proxy:
            from xoso66_cms_chrome import resolve_cms_chrome_by_device

            row = resolve_cms_chrome_by_device(cms_device)
            if not row:
                print(f"Không tìm thấy chrome profile CMS: {cms_device}", file=sys.stderr)
                return 1
            proxy = str(row.get("proxy") or "").strip()
            if not proxy:
                print(f"Profile {cms_device} thiếu proxy trong CMS", file=sys.stderr)
                return 1
            print(
                f"[REGISTER] CMS device {cms_device} | proxy {proxy.split(':')[0]}:{proxy.split(':')[1]}",
                flush=True,
            )
    if not proxy:
        print("Cần --proxy hoặc --cms-device", file=sys.stderr)
        parser.print_help()
        return 1

    try:
        guest = new_guest_session(proxy=proxy)
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
            result = register_account_playwright(plain, proxy=proxy)
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
