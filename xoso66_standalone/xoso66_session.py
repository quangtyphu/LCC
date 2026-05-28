# -*- coding: utf-8 -*-
"""
Quản lý session XOSO66 — mọi API nên đi qua ensure_session() trước.

Luồng:
  1. getBalance → session hợp lệ?
  2. Không → POST /user/login (encrypt) → lưu cookies/form_token
  3. Trả session dict dùng cho deposit / API khác

Dùng:
  from xoso66_session import ensure_session, get_user_balance

  Mọi GET getBalance — mặc định không in log (bật: XOSO66_LOG_GETBALANCE=1).

  session = ensure_session("acc1")
  # ... gọi deposit, v.v.
"""

from __future__ import annotations

import os
import time
import threading
from typing import Any, Callable

import requests

from xoso66_sessions_io import load_sessions, merge_account, save_sessions, use_db

BASE_URL = os.environ.get("XOSO66_BASE_URL", "https://v6sgqpyi.whskxk1.com").rstrip("/")
LOGIN_PATH = "/server/user/login"
GET_BALANCE_PATH = "/server/user/getBalance"
ENCRYPT_KEY_PATH = "/server/index/encryptKey"
LOGIN_CODE_2FA = 80080

def _getbalance_log_enabled() -> bool:
    """Mặc định tắt — bật: XOSO66_LOG_GETBALANCE=1."""
    return os.environ.get("XOSO66_LOG_GETBALANCE", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _username_for_getbalance_log(session: dict) -> str:
    """Nhãn log — ưu tiên username site."""
    for k in ("username", "login_name", "phone"):
        v = session.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    ui = session.get("user_info")
    if isinstance(ui, dict):
        u = str(ui.get("username") or "").strip()
        if u:
            return u
    sid = (
        str(session.get("id") or session.get("_balance_log_account_id") or "").strip()
    )
    if sid:
        return sid
    return "?"


def _format_balance_log_amount(v: Any) -> str:
    if v is None:
        return "—"
    try:
        x = float(v)
        return f"{x:,.0f}đ"
    except (TypeError, ValueError):
        return str(v)


def _log_getbalance_http_result(session: dict, result: dict[str, Any]) -> None:
    """Mỗi GET getBalance từ site — log thống nhất: Username - Balance xxx."""
    if not _getbalance_log_enabled():
        return
    name = _username_for_getbalance_log(session)
    u_api = result.get("username")
    if isinstance(u_api, str) and u_api.strip():
        name = u_api.strip()
    if result.get("reason") == "thiếu form_token":
        print(f"{name} - Balance (thiếu form_token)", flush=True)
        return
    if result.get("ok"):
        amt = _format_balance_log_amount(result.get("balance"))
        print(f"{name} - Balance {amt}", flush=True)
        return
    parts: list[str] = []
    raw = result.get("raw")
    if isinstance(raw, dict):
        m = str(raw.get("msg") or raw.get("message") or "").strip()
        if m:
            parts.append(m[:200])
    elif isinstance(raw, str) and raw.strip():
        rt = raw.strip()
        if "cloudflare" in rt.lower():
            parts.append("Cloudflare")
        else:
            parts.append(rt[:120])
    if not parts:
        hs = result.get("http_status")
        if hs is not None:
            parts.append(f"HTTP {hs}")
        elif result.get("need_cf_refresh"):
            parts.append("need_cf_refresh")
        else:
            parts.append("?")
    hint = " ".join(parts) or "getBalance thất bại"
    print(f"{name} - Balance (lỗi: {hint})", flush=True)


# Mã / msg thường gặp khi hết phiên (bổ sung khi gặp thêm)
SESSION_INVALID_CODES = {401, 403, 1001, 1002, 1020, 1021}


class SessionInvalidError(Exception):
    """Session không dùng được — cần login lại."""


def _requests_session(session: dict) -> requests.Session:
    """Session requests — bắt buộc SOCKS5 (session hoặc default config)."""
    from xoso66_proxy import apply_requests_proxy, ensure_proxy

    ensure_proxy(session)
    s = requests.Session()
    apply_requests_proxy(s, session["proxy"])
    return s


def _merge_response_cookies(session: dict, resp: requests.Response) -> None:
    cookies = dict(session.get("cookies") or {})
    for c in resp.cookies:
        cookies[c.name] = c.value
    session["cookies"] = cookies


def merge_playwright_cookies(session: dict, pw_cookies: Any) -> None:
    """Gộp cookie từ Playwright browser_context.cookies()."""
    cookies = dict(session.get("cookies") or {})
    for c in pw_cookies or []:
        if isinstance(c, dict) and c.get("name"):
            cookies[str(c["name"])] = str(c.get("value") or "")
    session["cookies"] = cookies


def is_session_valid_response(js: Any, *, http_status: int = 200) -> bool:
    """API JSON trả về có coi là đã login không."""
    if http_status in (401, 403):
        return False
    if not isinstance(js, dict):
        return False
    code = js.get("code")
    if code == 1:
        return True
    if code in SESSION_INVALID_CODES:
        return False
    msg = str(js.get("msg") or js.get("message") or "").lower()
    if any(x in msg for x in ("login", "đăng nhập", "dang nhap", "token", "phiên", "phien")):
        return False
    return False


def persist_session(account_id: str, session: dict) -> None:
    if use_db():
        from xoso66_accounts_db import save_session_runtime

        save_session_runtime(account_id, session)
        return
    accounts = load_sessions()
    accounts[account_id] = session
    save_sessions(accounts)


def prepare_login_payload(
    username: str,
    password: str,
    *,
    captcha: str = "",
    source: str = "",
) -> dict:
    body: dict[str, str] = {
        "username": str(username),
        "password": str(password),
        "captcha": str(captcha or ""),
    }
    if source:
        body["source"] = str(source)
    return body


def bootstrap_prelogin(session: dict, http: requests.Session | None = None) -> None:
    from xoso66_deposit import (
        DEFAULT_UA,
        _cookie_header,
        apply_response_tokens,
        fetch_cek_p,
        get_form_token,
    )

    session.pop("aes_session_key", None)
    http = http or _requests_session(session)
    headers = {
        "user-agent": session.get("user_agent") or DEFAULT_UA,
        "accept": "application/json",
        "cookie": _cookie_header(session),
    }
    try:
        r = http.get(f"{BASE_URL}{ENCRYPT_KEY_PATH}", headers=headers, timeout=25)
        apply_response_tokens(session, r.headers)
        if r.headers.get("cek-p") or r.headers.get("Cek-P"):
            session["cek_p"] = r.headers.get("cek-p") or r.headers.get("Cek-P")
        _merge_response_cookies(session, r)
    except Exception:
        pass
    try:
        fetch_cek_p(session)
    except Exception:
        pass
    try:
        get_form_token(session)
    except Exception:
        pass


def post_encrypted(
    session: dict,
    path: str,
    plain: dict,
    *,
    http: requests.Session | None = None,
) -> tuple[int, Any, dict]:
    from xoso66_deposit import (
        build_request_headers,
        crypto_available,
        decrypt_deposit_body,
        encrypt_deposit_body,
        get_form_token,
    )

    if not crypto_available():
        raise RuntimeError("pip install pycryptodome")

    form_token = get_form_token(session)
    encrypted_body, cek_k, aes_key = encrypt_deposit_body(session, plain)
    headers = build_request_headers(session, cek_k=cek_k, form_token=form_token)
    http = http or _requests_session(session)
    r = http.post(f"{BASE_URL}{path}", data=encrypted_body, headers=headers, timeout=45)
    text = r.text
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1]
    decrypted: Any = None
    if r.status_code == 200 and text:
        try:
            decrypted = decrypt_deposit_body(session, text, aes_key, dict(r.headers))
        except Exception as e:
            decrypted = {"_decrypt_error": str(e), "_cipher_preview": text[:200]}
    _merge_response_cookies(session, r)
    return r.status_code, decrypted, dict(r.headers)


def refresh_cloudflare(session: dict) -> dict[str, Any]:
    """Tự lấy cf_clearance + cf-* (curl_cffi rồi Playwright)."""
    from xoso66_cf import refresh_cloudflare as _refresh

    return _refresh(session)


def login_account(session: dict) -> dict:
    """POST /server/user/login — cập nhật session trong RAM."""
    username = session.get("username") or session.get("phone") or session.get("login_name")
    password = session.get("password") or session.get("login_pass")
    if not username or not password:
        raise ValueError('Thiếu "username" / "password" trong xoso66_sessions.json')

    cookies = session.get("cookies") or {}
    if not cookies.get("cf_clearance") or not (session.get("headers") or {}).get("cf-auth-token"):
        report = refresh_cloudflare(session)
        if not report.get("ok"):
            raise ValueError(
                "Không vượt được Cloudflare tự động. "
                f"Chi tiết: {report}. "
                "Chạy: pip install playwright && playwright install chromium"
            )

    http = _requests_session(session)
    bootstrap_prelogin(session, http=http)
    plain = prepare_login_payload(
        username,
        password,
        captcha=str(session.get("captcha") or ""),
        source=str(session.get("login_source") or ""),
    )
    status, data, _ = post_encrypted(session, LOGIN_PATH, plain, http=http)
    if status != 200:
        raise RuntimeError(f"Login HTTP {status}")
    if not isinstance(data, dict):
        raise RuntimeError(f"Login response lỗi: {data!r}")
    code = data.get("code")
    if code == LOGIN_CODE_2FA:
        raise RuntimeError("Tài khoản cần 2FA (code 80080)")
    if code != 1:
        msg = str(data.get("msg") or f"Login thất bại code={code}")
        from xoso66_account_errors import maybe_mark_account_loi_from_session

        maybe_mark_account_loi_from_session(session, msg, source="login")
        raise RuntimeError(msg)

    user_data = data.get("data") if isinstance(data.get("data"), dict) else {}
    return {
        "cookies": session.get("cookies"),
        "form_token": session.get("form_token"),
        "headers": session.get("headers"),
        "user_info": user_data,
        "login_raw": data,
    }


def get_user_balance(session: dict, *, refresh: bool = True) -> dict[str, Any]:
    """GET /server/user/getBalance — probe session + số dư."""
    from xoso66_deposit import apply_response_tokens, build_common_headers, get_form_token

    if not str(session.get("form_token") or "").strip():
        out = {"ok": False, "reason": "thiếu form_token"}
        _log_getbalance_http_result(session, out)
        return out
    form_token = get_form_token(session)
    headers = build_common_headers(
        session,
        form_token=form_token,
        content_type="application/x-www-form-urlencoded/json",
    )
    params = {"refresh": "1"} if refresh else {}
    r = _requests_session(session).get(
        f"{BASE_URL}{GET_BALANCE_PATH}",
        headers=headers,
        params=params,
        timeout=25,
    )
    apply_response_tokens(session, r.headers)
    _merge_response_cookies(session, r)
    if r.status_code in (401, 403) or "cloudflare" in (r.text or "")[:500].lower():
        out = {"ok": False, "http_status": r.status_code, "raw": r.text[:400], "need_cf_refresh": True}
        _log_getbalance_http_result(session, out)
        return out
    try:
        js = r.json()
    except Exception:
        out = {"ok": False, "http_status": r.status_code, "raw": r.text[:400], "need_cf_refresh": True}
        _log_getbalance_http_result(session, out)
        return out
    data = js.get("data") if isinstance(js.get("data"), dict) else {}
    balance = data.get("money") or data.get("balance") or data.get("total_money")
    ok = is_session_valid_response(js, http_status=r.status_code)
    out = {"ok": ok, "balance": balance, "username": data.get("username"), "raw": js}
    _log_getbalance_http_result(session, out)
    return out


def refresh_account_balance_to_db(
    account_id: str,
    session: dict | None = None,
    *,
    refresh: bool = True,
) -> dict[str, Any]:
    """GET getBalance → cập nhật accounts.balance + session_json (cookies/token)."""
    aid = str(account_id).strip()
    if session is None:
        session = ensure_session(aid, force_login=False)
    session.setdefault("_balance_log_account_id", aid)

    bal = get_user_balance(session, refresh=refresh)
    if not bal.get("ok"):
        raw = bal.get("raw")
        if isinstance(raw, dict):
            err = str(raw.get("msg") or raw.get("message") or "")
            from xoso66_account_errors import maybe_mark_account_loi_from_api

            maybe_mark_account_loi_from_api(
                session, raw, source="getBalance", account_id=aid
            )
        else:
            err = str(raw or bal.get("http_status") or "getBalance thất bại")
        return {"ok": False, "account_id": aid, "error": err.strip() or "getBalance thất bại"}

    balance_raw = bal.get("balance")
    balance_f: float | None = None
    if balance_raw is not None:
        try:
            balance_f = float(balance_raw)
        except (TypeError, ValueError):
            balance_f = None
        ui = session.get("user_info")
        if not isinstance(ui, dict):
            ui = {}
            session["user_info"] = ui
        ui["money"] = balance_f if balance_f is not None else balance_raw

    from xoso66_accounts_db import save_session_runtime

    save_session_runtime(aid, session)
    return {"ok": True, "account_id": aid, "balance": balance_f}


def session_is_valid(session: dict) -> bool:
    return bool(get_user_balance(session).get("ok"))


def prep_site_session_before_ws(account_id: str) -> None:
    """
    Trước mở WS: refresh getBalance; login lại nếu «Thông tin phiên không hợp lệ».
    Tránh mở WS khi site session (form_token) đã cũ sau «Đủ ngày».
    """
    aid = str(account_id or "").strip()
    if not aid:
        return
    try:
        session = ensure_session(aid, force_login=False)
        rep = refresh_account_balance_to_db(aid, session, refresh=True)
        if rep.get("ok"):
            return
        err = str(rep.get("error") or "").strip().lower()
        if "phiên" in err or "phien" in err or "session" in err or "login" in err:
            session = ensure_session(aid, force_login=True)
            refresh_account_balance_to_db(aid, session, refresh=True)
    except Exception:
        pass


def session_health(session: dict) -> dict[str, Any]:
    bal = get_user_balance(session)
    return {
        "ok": bool(bal.get("ok")),
        "balance": bal.get("balance"),
        "detail": bal.get("raw") if not bal.get("ok") else {"balance": bal.get("balance")},
    }


def ensure_session(account_id: str, *, force_login: bool = False) -> dict:
    """
    Trả session sẵn sàng gọi API.
    getBalance fail → refresh Cloudflare (auto) → login → lưu file.
    """
    accounts = load_sessions()
    if account_id not in accounts:
        raise KeyError(f"Không có account '{account_id}' trong xoso66_sessions.json")
    acc = accounts[account_id]
    acc.setdefault("id", account_id)
    from xoso66_proxy import ensure_proxy

    ensure_proxy(acc)

    has_token = bool(str(acc.get("form_token") or "").strip())
    if not force_login and has_token and session_is_valid(acc):
        persist_session(account_id, acc)
        return acc

    if has_token:
        bal_probe = get_user_balance(acc)
        if bal_probe.get("need_cf_refresh") or not (acc.get("cookies") or {}).get("cf_clearance"):
            refresh_cloudflare(acc)
    elif not (acc.get("cookies") or {}).get("cf_clearance"):
        refresh_cloudflare(acc)

    def _try_login() -> None:
        merge_account(acc, login_account(acc))

    try:
        _try_login()
    except Exception:
        refresh_cloudflare(acc)
        _try_login()

    persist_session(account_id, acc)
    if not session_is_valid(acc):
        refresh_cloudflare(acc)
        if not session_is_valid(acc):
            raise SessionInvalidError(
                "Sau login + refresh CF vẫn không getBalance — thử: "
                "python xoso66_login.py -a acc1 --refresh-cf"
            )
    return acc


def with_session(account_id: str, fn: Callable[[dict], Any], *, force_login: bool = False) -> Any:
    """Helper: ensure_session rồi gọi fn(session)."""
    session = ensure_session(account_id, force_login=force_login)
    return fn(session)
