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

# Cookie định danh phiên user — không copy giữa acc / không lấy từ CF warm.
SESSION_IDENTITY_COOKIE_NAMES = frozenset({"PHPSESSID"})


def strip_identity_cookies(session: dict) -> None:
    """Xóa PHPSESSID (và cookie định danh khác) khỏi session dict."""
    cookies = dict(session.get("cookies") or {})
    changed = False
    for name in SESSION_IDENTITY_COOKIE_NAMES:
        if name in cookies:
            cookies.pop(name, None)
            changed = True
    if changed:
        session["cookies"] = cookies


def merge_session_cookies(
    session: dict,
    incoming: dict[str, Any] | None,
    *,
    allow_identity: bool = True,
) -> None:
    """
    Gộp cookie vào session.
    allow_identity=False — bỏ PHPSESSID (CF warm / copy từ acc khác).
    """
    cookies = dict(session.get("cookies") or {})
    for name, value in (incoming or {}).items():
        if not name or value is None:
            continue
        key = str(name)
        if not allow_identity and key in SESSION_IDENTITY_COOKIE_NAMES:
            continue
        cookies[key] = str(value)
    session["cookies"] = cookies

# Session quá hạn → login lại.
SESSION_MAX_AGE_SEC = int(os.environ.get("XOSO66_SESSION_MAX_AGE_SEC", str(6 * 3600)))
# Tránh spam POST /login khi WS resync dồn (site: «lặp lại quá thường xuyên»).
LOGIN_ATTEMPT_COOLDOWN_SEC = int(
    os.environ.get("XOSO66_LOGIN_ATTEMPT_COOLDOWN_SEC", "90")
)
_LOGIN_ATTEMPT_LAST: dict[str, float] = {}
_LOGIN_ATTEMPT_LOCK = threading.Lock()


def _login_cooldown_remaining(account_id: str) -> float:
    key = str(account_id or "").strip()
    if not key:
        return 0.0
    with _LOGIN_ATTEMPT_LOCK:
        last = _LOGIN_ATTEMPT_LAST.get(key, 0.0)
    return max(0.0, LOGIN_ATTEMPT_COOLDOWN_SEC - (time.time() - last))


def _mark_login_attempt(account_id: str) -> None:
    key = str(account_id or "").strip()
    if not key:
        return
    with _LOGIN_ATTEMPT_LOCK:
        _LOGIN_ATTEMPT_LAST[key] = time.time()


def _session_needs_relogin(session: dict) -> bool:
    """True nếu đã quá SESSION_MAX_AGE_SEC kể từ lần login gần nhất."""
    ts = session.get("session_login_at")
    if ts is None:
        # Acc cũ chưa có stamp — login 1 lần để tránh money stale.
        return True
    try:
        age = time.time() - float(ts)
    except (TypeError, ValueError):
        return True
    return age > SESSION_MAX_AGE_SEC


def _mark_session_logged_in(session: dict) -> None:
    session["session_login_at"] = time.time()


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


_SYNC_CHROME_LAST: dict[str, float] = {}
_SYNC_CHROME_LOCK = threading.Lock()
SYNC_CHROME_COOLDOWN_SEC = int(os.environ.get("XOSO66_SYNC_CHROME_COOLDOWN_SEC", "60"))


def _sync_chrome_cooldown_remaining(device: str) -> float:
    key = str(device or "").strip().upper()
    if not key:
        return 0.0
    with _SYNC_CHROME_LOCK:
        last = _SYNC_CHROME_LAST.get(key, 0.0)
    return max(0.0, SYNC_CHROME_COOLDOWN_SEC - (time.time() - last))


def _mark_sync_chrome(device: str) -> None:
    key = str(device or "").strip().upper()
    if not key:
        return
    with _SYNC_CHROME_LOCK:
        _SYNC_CHROME_LAST[key] = time.time()


def _requests_session(session: dict) -> Any:
    """Session requests — bắt buộc SOCKS5 + retry/báo Lỗi proxy khi SOCKS chết."""
    from xoso66_proxy import apply_requests_proxy, ensure_proxy, wrap_requests_session

    ensure_proxy(session)
    s = requests.Session()
    apply_requests_proxy(s, session["proxy"])
    return wrap_requests_session(s, session, source="HTTP")


def _merge_response_cookies(session: dict, resp: requests.Response) -> None:
    """Gộp Set-Cookie từ response HTTP của chính session này (được phép PHPSESSID mới)."""
    incoming = {c.name: c.value for c in resp.cookies}
    merge_session_cookies(session, incoming, allow_identity=True)


def merge_playwright_cookies(session: dict, pw_cookies: Any) -> None:
    """Gộp cookie từ Playwright browser_context.cookies()."""
    incoming: dict[str, Any] = {}
    for c in pw_cookies or []:
        if isinstance(c, dict) and c.get("name"):
            incoming[str(c["name"])] = str(c.get("value") or "")
    merge_session_cookies(session, incoming, allow_identity=True)


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
    """Tự lấy cf-* headers (+ cf_clearance nếu có)."""
    from xoso66_cf import refresh_cloudflare as _refresh

    return _refresh(session)


def login_account(session: dict) -> dict:
    """POST /server/user/login — cập nhật session trong RAM (tự giải captcha nếu code 1011)."""
    from xoso66_cf import (
        CfRateLimitError,
        cf_rate_limit_message,
        cf_rate_limit_remaining,
        is_cf_rate_limited,
        session_cf_ready,
    )
    from xoso66_captcha_solver import (
        captcha_base64_from_payload,
        captcha_enabled,
        is_wrong_captcha_response,
        load_captcha_config,
        solve_image_captcha_auto,
    )

    username = session.get("username") or session.get("phone") or session.get("login_name")
    password = session.get("password") or session.get("login_pass")
    if not username or not password:
        raise ValueError('Thiếu "username" / "password" trong xoso66_sessions.json')

    # Tránh dính PHPSESSID của nick khác (cookie pollute → getBalance trả số dư lẫn).
    strip_identity_cookies(session)

    if not session_cf_ready(session):
        if is_cf_rate_limited(session):
            raise CfRateLimitError(
                cf_rate_limit_message(session),
                remaining_sec=cf_rate_limit_remaining(session),
            )
        report = refresh_cloudflare(session)
        if report.get("rate_limited"):
            raise CfRateLimitError(
                cf_rate_limit_message(session),
                remaining_sec=cf_rate_limit_remaining(session),
            )
        if not report.get("ok"):
            raise ValueError(
                "Không vượt được Cloudflare tự động. "
                f"Chi tiết: {report}. "
                "Chạy: pip install playwright && playwright install chromium"
            )

    http = _requests_session(session)
    bootstrap_prelogin(session, http=http)

    cap_cfg = load_captcha_config()
    max_attempts = max(1, int(cap_cfg.get("max_attempts") or 3))
    captcha_text = str(session.get("captcha") or "")
    data: Any = None
    code: Any = None
    msg = ""

    for attempt in range(max_attempts):
        plain = prepare_login_payload(
            username,
            password,
            captcha=captcha_text,
            source=str(session.get("login_source") or ""),
        )
        status, data, _ = post_encrypted(session, LOGIN_PATH, plain, http=http)
        if status != 200:
            raise RuntimeError(f"Login HTTP {status}")
        if not isinstance(data, dict):
            raise RuntimeError(f"Login response lỗi: {data!r}")
        code = data.get("code")
        msg = str(data.get("msg") or "")
        if code == LOGIN_CODE_2FA:
            raise RuntimeError("Tài khoản cần 2FA (code 80080)")
        if code == 1:
            user_data = data.get("data") if isinstance(data.get("data"), dict) else {}
            session.pop("captcha", None)
            return {
                "cookies": session.get("cookies"),
                "form_token": session.get("form_token"),
                "headers": session.get("headers"),
                "user_info": user_data,
                "login_raw": data,
            }

        # Sai / thiếu captcha → Capsolver + retry
        if (
            not is_wrong_captcha_response(code, msg)
            or attempt + 1 >= max_attempts
            or not captcha_enabled()
        ):
            break

        b64 = captcha_base64_from_payload(data)
        if not b64:
            try:
                from xoso66_register import get_captcha

                cap = get_captcha(session)
                b64 = captcha_base64_from_payload(cap.get("raw") or {})
            except Exception:
                b64 = ""
        if not b64:
            break

        solved = solve_image_captcha_auto(b64)
        if not solved.get("ok"):
            print(
                f"[LOGIN] Captcha Capsolver fail: {solved.get('error')}",
                flush=True,
            )
            break
        captcha_text = str(solved.get("text") or "").strip()
        session["captcha"] = captcha_text
        print(
            f"[LOGIN] {username} captcha retry {attempt + 1}/{max_attempts}: {captcha_text!r}",
            flush=True,
        )

    fail_msg = msg or f"Login thất bại code={code}"
    from xoso66_account_errors import maybe_mark_account_loi_from_session

    maybe_mark_account_loi_from_session(session, fail_msg, source="login")
    raise RuntimeError(fail_msg)


def get_user_balance(session: dict, *, refresh: bool = True) -> dict[str, Any]:
    """GET /server/user/getBalance — probe session + số dư."""
    from xoso66_cf import is_cloudflare_rate_limited, mark_cf_rate_limited
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
    if is_cloudflare_rate_limited(r.text or "", r.status_code):
        mark_cf_rate_limited(session)
        out = {
            "ok": False,
            "http_status": r.status_code,
            "raw": r.text[:400],
            "rate_limited": True,
            "need_cf_refresh": True,
        }
        _log_getbalance_http_result(session, out)
        return out
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


def _as_money(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def refresh_account_balance_to_db(
    account_id: str,
    session: dict | None = None,
    *,
    refresh: bool = True,
    force_relogin: bool = False,
) -> dict[str, Any]:
    """GET getBalance → cập nhật accounts.balance + session_json (cookies/token).

    Tin số dư từ API; force_relogin chỉ dùng khi caller chủ động yêu cầu login.
    """
    aid = str(account_id).strip()
    from xoso66_accounts_db import save_session_runtime

    if force_relogin:
        session = ensure_session(aid, force_login=True)
    elif session is None:
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

    balance_f = _as_money(bal.get("balance"))
    if balance_f is not None:
        ui = session.get("user_info")
        if not isinstance(ui, dict):
            ui = {}
            session["user_info"] = ui
        ui["money"] = balance_f
    elif bal.get("balance") is not None:
        ui = session.get("user_info")
        if not isinstance(ui, dict):
            ui = {}
            session["user_info"] = ui
        ui["money"] = bal.get("balance")

    save_session_runtime(aid, session)
    return {
        "ok": True,
        "account_id": aid,
        "balance": balance_f,
        "relogin_verified": bool(force_relogin),
    }


def session_is_valid(session: dict) -> bool:
    return bool(get_user_balance(session).get("ok"))


def prep_site_session_before_ws(account_id: str) -> bool:
    """
    Trước mở WS: đảm bảo session + getBalance.
    Tôn trọng TTL SESSION_MAX_AGE_SEC — chỉ login khi hết hạn hoặc getBalance fail.
    Tin số dư API; nếu < ngưỡng WS → False.
    """
    aid = str(account_id or "").strip()
    if not aid:
        return False
    try:
        from xoso66_accounts_db import (
            STATUS_HET_TIEN,
            get_account,
            set_account_status,
            username_for_log,
        )
        from xoso66_config_util import load_config
        from xoso66_ws_pool import min_balance_for_ws

        row = get_account(aid) or {}
        status = str(row.get("status") or "").strip()
        min_bal = float(min_balance_for_ws(load_config()))

        session = ensure_session(aid, force_login=False)
        rep = refresh_account_balance_to_db(aid, session, refresh=True)
        if not rep.get("ok"):
            return False
        bal = _as_money(rep.get("balance"))
        if bal is not None and bal < min_bal:
            user = username_for_log(aid)
            print(
                f"[WS-POOL] {user}: sau check số dư {bal:,.0f} < {min_bal:,.0f} "
                f"— không mở WS",
                flush=True,
            )
            if status != STATUS_HET_TIEN:
                set_account_status(aid, STATUS_HET_TIEN, reason="thiếu tiền trước mở WS")
            return False
        return True
    except Exception:
        return False


def session_health(session: dict) -> dict[str, Any]:
    bal = get_user_balance(session)
    return {
        "ok": bool(bal.get("ok")),
        "balance": bal.get("balance"),
        "detail": bal.get("raw") if not bal.get("ok") else {"balance": bal.get("balance")},
    }


def sync_session_from_chrome(
    account_id: str,
    *,
    device: str = "",
    force_login: bool = False,
    timeout_sec: int = 0,
) -> dict[str, Any]:
    """
    Đồng bộ cf_clearance + cf-* từ Chrome CMS profile vào session DB.

    Luồng:
      1) Đọc cookie từ chrome_profiles_data (hoặc chờ CF trong Chrome đang mở)
      2) Sniff cf-auth-token qua Chrome tạm + cookie đã sync
      3) Login (nếu force_login) → lưu session + balance
    """
    from pathlib import Path

    from xoso66_accounts_db import get_account, update_account
    from xoso66_cms_chrome import device_proxy_mismatch, resolve_cms_chrome_by_device
    from xoso66_chrome_profile import (
        read_profile_cookies_after_close,
        warm_session_from_profile,
    )
    from xoso66_cf import (
        cf_rate_limit_message,
        cf_rate_limit_remaining,
        is_cf_rate_limited,
    )

    aid = str(account_id or "").strip()
    if not aid:
        raise ValueError("account_id trống")

    row = get_account(aid)
    if not row:
        raise KeyError(aid)

    accounts = load_sessions()
    if aid not in accounts:
        raise KeyError(aid)
    session = accounts[aid]
    session.setdefault("id", aid)

    dev = str(device or row.get("device") or session.get("device") or "").strip()
    if not dev:
        raise ValueError("Thiếu device CMS (vd. XMSB76) — gán cột Device rồi thử lại")

    cms = resolve_cms_chrome_by_device(dev)
    if not cms:
        raise ValueError(f"Không tìm thấy Chrome CMS device '{dev}' trong game_data.db")

    profile_dir = Path(str(cms.get("profile_dir") or "").strip())
    if not profile_dir.is_dir():
        raise ValueError(f"Profile Chrome không tồn tại: {profile_dir}")

    cms_proxy = str(cms.get("proxy") or "").strip()
    if not cms_proxy:
        raise ValueError(f"Chrome {dev} thiếu proxy trong game_data.db")

    mismatch = device_proxy_mismatch({**row, "device": dev})
    meta: dict[str, Any] = {
        "account_id": aid,
        "device": dev,
        "profile_dir": str(profile_dir),
    }
    if mismatch:
        meta["proxy_mismatch"] = mismatch
        session["proxy"] = cms_proxy
        print(f"[SYNC-CHROME] {dev}: dùng proxy CMS — {cms_proxy[:40]}…", flush=True)
    elif not str(session.get("proxy") or "").strip():
        session["proxy"] = cms_proxy

    if is_cf_rate_limited(cms_proxy) or is_cf_rate_limited(session):
        px = cms_proxy or str(session.get("proxy") or "")
        return {
            "ok": False,
            "rate_limited": True,
            "error": "cf_rate_limited",
            "msg": cf_rate_limit_message(px),
            "remaining_sec": int(cf_rate_limit_remaining(px)),
            **meta,
        }

    sync_wait = _sync_chrome_cooldown_remaining(dev)
    if sync_wait > 0:
        return {
            "ok": False,
            "error": "sync_cooldown",
            "msg": f"Chờ {int(sync_wait)}s trước khi sync Chrome lại (tránh rate limit).",
            "retry_after_sec": int(sync_wait),
            **meta,
        }
    _mark_sync_chrome(dev)

    to = int(timeout_sec or os.environ.get("XOSO66_CF_MANUAL_WAIT_SEC", "30"))

    def _probe_fail_hint(probe_out: dict[str, Any]) -> str:
        detail = probe_out.get("detail") if isinstance(probe_out.get("detail"), dict) else {}
        reason = str(
            detail.get("reason")
            or detail.get("error")
            or probe_out.get("error")
            or "api_probe_fail"
        )
        has_sess = bool((session.get("cookies") or {}).get("PHPSESSID"))
        has_ft = bool(str(session.get("form_token") or "").strip())
        if has_sess and not has_ft:
            return "có PHPSESSID nhưng chưa lấy được form_token (CF có thể chặn API)"
        if has_sess:
            return f"API từ chối session ({reason})"
        return "chưa có PHPSESSID — đăng nhập trong Chrome trước"

    def _sync_fail_response(probe_out: dict[str, Any], *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        names = sorted((session.get("cookies") or {}).keys())
        has_bm = bool((session.get("cookies") or {}).get("__cf_bm"))
        hint = _probe_fail_hint(probe_out)
        msg = f"Sync {dev} thất bại ({hint})."
        if names:
            msg += f" Cookie: {', '.join(names[:8])}."
        if has_bm and not (session.get("cookies") or {}).get("cf_clearance"):
            msg += " Thiếu cf_clearance — giải captcha trong Chrome rồi thử lại."
        out = {
            "ok": False,
            "error": "sync_probe_fail",
            "msg": msg,
            "cookie_names": names,
            "chrome_session_probe": probe_out,
            **meta,
        }
        if extra:
            out.update(extra)
        return out

    sync_deadline = time.time() + max(5, to)

    print(f"[SYNC-CHROME] {dev}: doc cookie tu profile…", flush=True)
    loaded = read_profile_cookies_after_close(session, profile_dir, allow_kill=False)
    meta["profile_cookies"] = loaded
    if loaded.get("restarted_chrome"):
        print(
            f"[SYNC-CHROME] {dev}: da dong Chrome de doc cookie"
            + (f" (kill {loaded.get('terminated_chrome')})" if loaded.get("terminated_chrome") else "")
            + ".",
            flush=True,
        )
    elif loaded.get("chrome_open"):
        print(
            f"[SYNC-CHROME] {dev}: Chrome dang mo — doc cookie khong kill browser.",
            flush=True,
        )

    cookie_names = sorted((session.get("cookies") or {}).keys())
    meta["cookie_names"] = cookie_names

    def _probe_site_session() -> dict[str, Any]:
        try:
            bootstrap_prelogin(session)
            bal = get_user_balance(session)
            if bal.get("rate_limited"):
                return {"ok": False, "rate_limited": True, "detail": bal}
            return {
                "ok": bool(bal.get("ok")),
                "balance": bal.get("balance"),
                "detail": bal,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    probe = _probe_site_session()
    meta["chrome_session_probe"] = probe
    if probe.get("rate_limited"):
        return {
            "ok": False,
            "rate_limited": True,
            "error": "cf_rate_limited",
            "msg": cf_rate_limit_message(cms_proxy),
            "remaining_sec": int(cf_rate_limit_remaining(cms_proxy)),
            **meta,
        }
    if probe.get("ok"):
        print(
            f"[SYNC-CHROME] {dev}: cookie Chrome du de goi API (balance={probe.get('balance')})",
            flush=True,
        )
        if dev != str(row.get("device") or "").strip():
            update_account(aid, {"device": dev})
        persist_session(aid, session)
        bal_rep = refresh_account_balance_to_db(aid, session, refresh=True)
        balance = bal_rep.get("balance") if bal_rep.get("ok") else probe.get("balance")
        return {
            "ok": True,
            "account_id": aid,
            "device": dev,
            "balance": balance,
            "has_clearance": bool((session.get("cookies") or {}).get("cf_clearance")),
            "has_cf_headers": bool((session.get("headers") or {}).get("cf-auth-token")),
            "sync_via": "chrome_cookies",
            **meta,
        }

    # Probe fail — fail nhanh (timeout mặc định 30s, không chờ CF / mở Chrome lại)
    if time.time() < sync_deadline and loaded.get("cookie_names"):
        rem = min(3.0, sync_deadline - time.time())
        if rem >= 1.0:
            time.sleep(rem)
            warm_session_from_profile(session, profile_dir, allow_identity=True)
            probe_retry = _probe_site_session()
            meta["chrome_session_probe_retry"] = probe_retry
            if probe_retry.get("rate_limited"):
                return {
                    "ok": False,
                    "rate_limited": True,
                    "error": "cf_rate_limited",
                    "msg": cf_rate_limit_message(cms_proxy),
                    "remaining_sec": int(cf_rate_limit_remaining(cms_proxy)),
                    **meta,
                }
            if probe_retry.get("ok"):
                if dev != str(row.get("device") or "").strip():
                    update_account(aid, {"device": dev})
                persist_session(aid, session)
                bal_rep = refresh_account_balance_to_db(aid, session, refresh=True)
                balance = bal_rep.get("balance") if bal_rep.get("ok") else probe_retry.get("balance")
                return {
                    "ok": True,
                    "account_id": aid,
                    "device": dev,
                    "balance": balance,
                    "has_clearance": bool((session.get("cookies") or {}).get("cf_clearance")),
                    "has_cf_headers": bool((session.get("headers") or {}).get("cf-auth-token")),
                    "sync_via": "chrome_cookies_retry",
                    **meta,
                }
            probe = probe_retry

    return _sync_fail_response(probe)


def ensure_session(
    account_id: str,
    *,
    force_login: bool = False,
    ignore_session_ttl: bool = False,
) -> dict:
    """
    Trả session sẵn sàng gọi API.
    getBalance fail → refresh Cloudflare (auto) → login → lưu file.
    ignore_session_ttl=True: bỏ qua TTL 6h (chỉ dùng khi mission/list stale).
    """
    from xoso66_cf import (
        CfRateLimitError,
        cf_rate_limit_message,
        cf_rate_limit_remaining,
        is_cf_rate_limited,
        session_cf_ready,
    )

    accounts = load_sessions()
    if account_id not in accounts:
        raise KeyError(f"Không có account '{account_id}' trong xoso66_sessions.json")
    acc = accounts[account_id]
    acc.setdefault("id", account_id)
    from xoso66_proxy import ensure_proxy

    ensure_proxy(acc)

    if is_cf_rate_limited(acc):
        raise CfRateLimitError(
            cf_rate_limit_message(acc),
            remaining_sec=cf_rate_limit_remaining(acc),
        )

    has_token = bool(str(acc.get("form_token") or "").strip())
    needs_relogin = _session_needs_relogin(acc)

    if not force_login and needs_relogin:
        force_login = True

    # Session còn TTL: probe getBalance trước — bỏ qua nếu force_login.
    if has_token and not needs_relogin and not ignore_session_ttl and not force_login:
        bal_probe = get_user_balance(acc)
        if bal_probe.get("rate_limited"):
            raise CfRateLimitError(
                cf_rate_limit_message(acc),
                remaining_sec=cf_rate_limit_remaining(acc),
            )
        if bal_probe.get("ok"):
            persist_session(account_id, acc)
            return acc

    if not force_login and has_token and session_is_valid(acc):
        persist_session(account_id, acc)
        return acc

    def _maybe_refresh_cf() -> None:
        if is_cf_rate_limited(acc):
            raise CfRateLimitError(
                cf_rate_limit_message(acc),
                remaining_sec=cf_rate_limit_remaining(acc),
            )
        if session_cf_ready(acc):
            return
        report = refresh_cloudflare(acc)
        if report.get("rate_limited"):
            raise CfRateLimitError(
                cf_rate_limit_message(acc),
                remaining_sec=cf_rate_limit_remaining(acc),
            )

    if not session_cf_ready(acc):
        if has_token and not force_login:
            bal_probe = get_user_balance(acc)
            if bal_probe.get("rate_limited"):
                raise CfRateLimitError(
                    cf_rate_limit_message(acc),
                    remaining_sec=cf_rate_limit_remaining(acc),
                )
            if bal_probe.get("ok"):
                persist_session(account_id, acc)
                return acc
        _maybe_refresh_cf()

    def _try_login() -> None:
        cooldown_rem = _login_cooldown_remaining(account_id)
        # force_login=True: bỏ cooldown (caller chủ động yêu cầu login).
        if cooldown_rem > 0 and not needs_relogin and not force_login:
            bal = get_user_balance(acc)
            if bal.get("ok"):
                return
            raise RuntimeError(
                f"Login cooldown ~{int(cooldown_rem)}s — getBalance vẫn fail"
            )
        _mark_login_attempt(account_id)
        merge_account(acc, login_account(acc))
        _mark_session_logged_in(acc)

    try:
        _try_login()
    except CfRateLimitError:
        raise
    except Exception:
        if not is_cf_rate_limited(acc):
            refresh_cloudflare(acc)
        _try_login()

    persist_session(account_id, acc)
    if not session_is_valid(acc):
        bal = get_user_balance(acc)
        if bal.get("ok"):
            persist_session(account_id, acc)
            return acc
        if is_cf_rate_limited(acc):
            raise CfRateLimitError(
                cf_rate_limit_message(acc),
                remaining_sec=cf_rate_limit_remaining(acc),
            )
        if not session_cf_ready(acc):
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
