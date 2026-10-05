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

import json
import os
import re
import time
import threading
from typing import Any, Callable

import requests

from xoso66_sessions_io import (
    apply_session_merge,
    load_sessions,
    merge_account,
    save_sessions,
    use_db,
)

from xoso66_game_domain import default_base_url, resolve_base_url
BASE_URL = default_base_url()  # legacy; prefer resolve_base_url(session)
LOGIN_PATH = "/server/user/login"
GET_BALANCE_PATH = "/server/user/getBalance"
ENCRYPT_KEY_PATH = "/server/index/encryptKey"
LOGIN_CODE_2FA = 80080
# Cloudflare chặn POST /user/login (475) — thường khi gửi captcha / POST liên tiếp.
LOGIN_CF_BLOCK_HTTP_STATUSES = frozenset({475, 403})
LOGIN_CF_RETRIES = max(1, int(os.environ.get("XOSO66_LOGIN_CF_RETRIES", "2")))

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
# Sau CF 475 / site «lặp lại quá thường xuyên» — chờ lâu hơn trước khi login lại.
LOGIN_CF_BACKOFF_SEC = int(os.environ.get("XOSO66_LOGIN_CF_BACKOFF_SEC", "180"))
LOGIN_SPAM_BACKOFF_SEC = int(os.environ.get("XOSO66_LOGIN_SPAM_BACKOFF_SEC", "300"))
# Chỉ 1 login HTTP/PW tại một thời điểm — tránh 16 WS mở → 16 login song song → 475.
LOGIN_GLOBAL_CONCURRENCY = max(1, int(os.environ.get("XOSO66_LOGIN_GLOBAL_CONCURRENCY", "1")))
# HTTP 475 → login Chrome UI (xoso66_chrome_manual_login). Tắt: XOSO66_CF475_CHROME_UI=0
CF475_CHROME_UI_ENABLED = os.environ.get("XOSO66_CF475_CHROME_UI", "1").strip().lower() in (
    "1",
    "true",
    "yes",
)
CF475_CHROME_UI_TIMEOUT_SEC = int(
    os.environ.get("XOSO66_CF475_CHROME_UI_TIMEOUT_SEC", "180")
)
_CHROME_UI_RECOVER_LOCK = threading.Lock()
_LOGIN_ATTEMPT_LAST: dict[str, float] = {}
_LOGIN_BLOCKED_UNTIL: dict[str, float] = {}
_LOGIN_ATTEMPT_LOCK = threading.Lock()
_LOGIN_GATE = threading.Semaphore(LOGIN_GLOBAL_CONCURRENCY)


class LoginBlockedError(RuntimeError):
    """Acc đang backoff login (CF 475 / site rate limit) — không đánh STATUS_LỖI."""

    def __init__(self, message: str = "", *, remaining_sec: float = 0) -> None:
        super().__init__(message)
        self.remaining_sec = remaining_sec


def _login_cooldown_remaining(account_id: str) -> float:
    key = str(account_id or "").strip()
    if not key:
        return 0.0
    with _LOGIN_ATTEMPT_LOCK:
        last = _LOGIN_ATTEMPT_LAST.get(key, 0.0)
    return max(0.0, LOGIN_ATTEMPT_COOLDOWN_SEC - (time.time() - last))


def _login_blocked_remaining(account_id: str) -> float:
    key = str(account_id or "").strip()
    if not key:
        return 0.0
    with _LOGIN_ATTEMPT_LOCK:
        until = _LOGIN_BLOCKED_UNTIL.get(key, 0.0)
    return max(0.0, until - time.time())


def _mark_login_attempt(account_id: str) -> None:
    key = str(account_id or "").strip()
    if not key:
        return
    with _LOGIN_ATTEMPT_LOCK:
        _LOGIN_ATTEMPT_LAST[key] = time.time()


def _mark_login_blocked(account_id: str, *, sec: int | None = None) -> float:
    """Chặn login acc trong `sec` giây. Trả remaining."""
    key = str(account_id or "").strip()
    wait = max(30, int(sec if sec is not None else LOGIN_CF_BACKOFF_SEC))
    until = time.time() + wait
    if not key:
        return float(wait)
    with _LOGIN_ATTEMPT_LOCK:
        prev = _LOGIN_BLOCKED_UNTIL.get(key, 0.0)
        _LOGIN_BLOCKED_UNTIL[key] = max(prev, until)
        _LOGIN_ATTEMPT_LAST[key] = time.time()
        rem = max(0.0, _LOGIN_BLOCKED_UNTIL[key] - time.time())
    return rem


def _clear_login_blocked(account_id: str) -> None:
    key = str(account_id or "").strip()
    if not key:
        return
    with _LOGIN_ATTEMPT_LOCK:
        _LOGIN_BLOCKED_UNTIL.pop(key, None)


def _account_id_from_session(session: dict) -> str:
    return str(
        session.get("id") or session.get("_balance_log_account_id") or ""
    ).strip()


def _is_site_login_spam_msg(msg: str) -> bool:
    m = str(msg or "").strip().lower()
    return "lặp lại quá thường xuyên" in m


def _raise_login_blocked(account_id: str, reason: str, *, sec: int) -> None:
    rem = _mark_login_blocked(account_id, sec=sec)
    raise LoginBlockedError(
        f"{reason} — chờ ~{int(rem)}s rồi thử lại (không đánh Lỗi)",
        remaining_sec=rem,
    )


def _mark_account_loi_http_475(
    session: dict, account_id: str = "", *, status: int = 475
) -> None:
    """getBalance fail → login gặp CF 475 → ép status Lỗi, bỏ khỏi pool."""
    from xoso66_accounts_db import STATUS_LOI, get_account, set_account_status, username_for_log

    aid = str(account_id or _account_id_from_session(session) or "").strip()
    if not aid:
        return
    row = get_account(aid) or {}
    u = username_for_log(aid, row or session)
    reason = f"login: HTTP {status} Cloudflare chặn POST /user/login"
    if str(row.get("status") or "").strip() != STATUS_LOI:
        set_account_status(aid, STATUS_LOI, reason=reason)
    else:
        print(f"[ACCOUNT] {u}: lỗi hệ thống (đã Lỗi) — {reason}", flush=True)
    try:
        from xoso66_auto_mission_reward import cancel_mission_claim_queue

        cancel_mission_claim_queue(aid)
    except Exception:
        pass
    _mark_login_blocked(aid, sec=LOGIN_CF_BACKOFF_SEC)


def _try_recover_login_cf_475_via_chrome_ui(
    session: dict, account_id: str = "", *, status: int = 475
) -> dict | None:
    """
    Đánh Lỗi rồi thử login qua Chrome UI (giống xoso66_chrome_manual_login.py).
    Trả dict login OK hoặc None.
    """
    from xoso66_accounts_db import username_for_log

    aid = str(account_id or _account_id_from_session(session) or "").strip()
    if not aid:
        return None
    _mark_account_loi_http_475(session, aid, status=status)
    if not CF475_CHROME_UI_ENABLED:
        return None
    u = username_for_log(aid, session)
    print(
        f"[LOGIN] {u}: HTTP {status} CF — thử Chrome UI login "
        f"(timeout={CF475_CHROME_UI_TIMEOUT_SEC}s)…",
        flush=True,
    )
    try:
        from xoso66_chrome_ext_login import login_account_via_extension

        with _CHROME_UI_RECOVER_LOCK:
            out = login_account_via_extension(
                aid, timeout_sec=CF475_CHROME_UI_TIMEOUT_SEC
            )
    except Exception as exc:
        print(f"[LOGIN] {u}: Chrome UI login exception — {exc}", flush=True)
        return None
    if not out.get("ok"):
        err = out.get("msg") or out.get("error") or out
        print(f"[LOGIN] {u}: Chrome UI login fail — {err}", flush=True)
        return None
    fresh = load_sessions().get(aid) or {}
    if fresh:
        apply_session_merge(session, fresh)
    bootstrap_prelogin(session)
    _clear_login_blocked(aid)
    print(
        f"[LOGIN] {u}: Chrome UI login OK — balance={out.get('balance')} "
        f"status={out.get('status')}",
        flush=True,
    )
    user_data = {}
    if isinstance(session.get("user_info"), dict):
        user_data = session["user_info"]
    return {
        "cookies": session.get("cookies"),
        "form_token": session.get("form_token"),
        "headers": session.get("headers"),
        "user_info": user_data,
        "login_raw": {"code": 1, "msg": "chrome_ui_login", "method": out.get("method")},
    }


def _handle_login_cf_block(
    session: dict, account_id: str = "", *, status: int = 475
) -> dict:
    """HTTP 475/403 — đánh Lỗi, thử Chrome UI; OK → dict login, không → raise."""
    recovered = _try_recover_login_cf_475_via_chrome_ui(
        session, account_id, status=status
    )
    if recovered is not None:
        return recovered
    raise RuntimeError(
        f"Login HTTP {status} (Cloudflare chặn POST /user/login) — đã đánh Lỗi"
    )


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


def _merge_any_response_cookies(session: dict, resp: Any) -> None:
    """Gộp cookie từ requests hoặc curl_cffi response."""
    try:
        incoming = {c.name: c.value for c in resp.cookies}
    except Exception:
        try:
            incoming = dict(resp.cookies or {})
        except Exception:
            incoming = {}
    if incoming:
        merge_session_cookies(session, incoming, allow_identity=True)


def _parse_encrypted_post_response(
    session: dict,
    *,
    status: int,
    text: str,
    aes_key: str,
    resp_headers: dict,
    req_headers: dict | None = None,
) -> Any:
    """Giải body POST encrypt — JSON thường, v2 pack, hoặc AES (giống post_encrypted)."""
    from xoso66_deposit import apply_response_tokens, decrypt_deposit_body
    from xoso66_secure_headers import (
        CRYPTO_VERSION_HEADER,
        CRYPTO_VERSION_V2,
        unpack_v2_ciphertext,
    )

    if resp_headers:
        apply_response_tokens(session, resp_headers)
    if status != 200 or not text:
        return None
    raw: Any = text
    if isinstance(raw, str) and raw.startswith('"') and raw.endswith('"'):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = raw[1:-1]
    if not raw:
        return None
    try:
        if str(raw).lstrip().startswith(("{", "[")):
            return json.loads(raw) if isinstance(raw, str) else raw
        body = str(raw)
        response_version = (
            resp_headers.get(CRYPTO_VERSION_HEADER)
            or resp_headers.get(CRYPTO_VERSION_HEADER.title())
            or (req_headers or {}).get(CRYPTO_VERSION_HEADER)
        )
        if response_version == CRYPTO_VERSION_V2:
            body = unpack_v2_ciphertext(body)
        return decrypt_deposit_body(session, body, aes_key, resp_headers)
    except Exception as e:
        preview = str(raw)[:200]
        return {"_decrypt_error": str(e), "_cipher_preview": preview}


def _decrypt_encrypted_http_body(
    session: dict, resp: Any, aes_key: str, *, text: str | None = None
) -> Any:
    return _parse_encrypted_post_response(
        session,
        status=int(getattr(resp, "status_code", 0) or 0),
        text=text if text is not None else (getattr(resp, "text", None) or ""),
        aes_key=aes_key,
        resp_headers=dict(getattr(resp, "headers", {}) or {}),
    )


def _login_response_is_crypto_glitch(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    if body.get("_decrypt_error"):
        return True
    err = str(body.get("_decrypt_error") or "").lower()
    return "padding" in err or "decrypt" in err


def _login_use_cffi() -> bool:
    return os.environ.get("XOSO66_LOGIN_USE_CFFI", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


_LOGIN_FETCH_POST_JS = """async ([url, headers, body]) => {
    const r = await fetch(url, {
        method: 'POST',
        headers,
        body,
        credentials: 'include',
    });
    const text = await r.text();
    const out = {};
    r.headers.forEach((v, k) => { out[k] = v; });
    return { status: r.status, text, headers: out };
}"""


def _login_via_playwright(session: dict, plain: dict) -> tuple[int, Any, dict]:
    """
    POST login encrypt qua fetch() trong Playwright — tránh CF HTTP 475 (requests/cffi).
    Fallback Vue store nếu XOSO66_LOGIN_PLAYWRIGHT_MODE=vue.
    """
    from xoso66_deposit import (
        build_request_headers,
        encrypt_deposit_body,
        get_form_token,
    )

    mode = os.environ.get("XOSO66_LOGIN_PLAYWRIGHT_MODE", "fetch").strip().lower()
    if mode == "vue":
        return _login_via_playwright_vue(session, plain)

    form_token = get_form_token(session)
    encrypted_body, cek_k, aes_key = encrypt_deposit_body(session, plain)
    headers = build_request_headers(session, cek_k=cek_k, form_token=form_token)
    url = f"{resolve_base_url(session)}{LOGIN_PATH}"

    from xoso66_cf import _inject_session_cookies, attach_cf_request_sniffer
    from xoso66_playwright_ctx import playwright_browser

    _base = resolve_base_url(session)
    headless = os.environ.get("XOSO66_CF_HEADLESS", "1") != "0"
    fetch_js: Any = None
    with playwright_browser(
        session, base_url=_base, headless=headless
    ) as (_p, _browser, context):
        host = _base.split("//", 1)[-1].split("/", 1)[0]
        _inject_session_cookies(context, session, host)
        page = context.new_page()
        attach_cf_request_sniffer(page, session)
        page.goto(f"{_base}/home/", wait_until="domcontentloaded", timeout=90_000)
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            page.wait_for_timeout(6_000)
        page.wait_for_timeout(2_000)
        fetch_js = page.evaluate(
            _LOGIN_FETCH_POST_JS,
            [url, headers, encrypted_body],
        )
        merge_playwright_cookies(session, context.cookies())

    if not isinstance(fetch_js, dict):
        return 0, {"code": 0, "msg": "playwright fetch lỗi"}, {}
    status = int(fetch_js.get("status") or 0)
    resp_headers = fetch_js.get("headers") if isinstance(fetch_js.get("headers"), dict) else {}
    text = str(fetch_js.get("text") or "")
    if resp_headers:
        from xoso66_deposit import apply_response_tokens

        apply_response_tokens(session, resp_headers)
    decrypted: Any = None
    if status == 200 and text:
        decrypted = _parse_encrypted_post_response(
            session,
            status=status,
            text=text,
            aes_key=aes_key,
            resp_headers=resp_headers,
            req_headers=headers,
        )
    return status, decrypted, resp_headers


def _login_via_playwright_vue(session: dict, plain: dict) -> tuple[int, Any, dict]:
    """Login qua Vue store — dùng khi fetch không decrypt được."""
    from xoso66_captcha_solver import LOGIN_DISPATCH_JS
    from xoso66_cf import attach_cf_request_sniffer, bootstrap_register_page
    from xoso66_playwright_ctx import playwright_browser

    _base = resolve_base_url(session)
    headless = os.environ.get("XOSO66_CF_HEADLESS", "1") != "0"
    pw_js: Any = None
    with playwright_browser(session, base_url=_base, headless=headless) as (
        _p,
        _browser,
        context,
    ):
        page = context.new_page()
        attach_cf_request_sniffer(page, session)
        boot = bootstrap_register_page(
            page, session, context=context, headless=headless
        )
        if not boot.get("ok"):
            msg = str(boot.get("msg") or boot.get("error") or "bootstrap_failed")
            return 0, {"code": 0, "msg": msg}, {}
        pw_js = page.evaluate(LOGIN_DISPATCH_JS, plain)
        merge_playwright_cookies(session, context.cookies())

    if not isinstance(pw_js, dict):
        return 0, {"code": 0, "msg": "playwright response không hợp lệ"}, {}
    if pw_js.get("error") == "no_vue_store":
        return 0, {"code": 0, "msg": "no_vue_store"}, {}
    data = pw_js.get("response") if isinstance(pw_js.get("response"), dict) else pw_js
    if not isinstance(data, dict):
        err = str(pw_js.get("message") or pw_js.get("error") or "playwright login lỗi")
        return 0, {"code": 0, "msg": err}, {}
    return 200, data, {}


def _submit_login(
    session: dict,
    plain: dict,
    *,
    http: requests.Session | None = None,
    prefer_playwright: bool = False,
    cf_retry: int = 0,
) -> tuple[int, Any, dict]:
    """
    Gửi login.
    prefer_playwright=False → HTTP/curl_cffi (kể cả có captcha — giống form web).
    prefer_playwright=True → Playwright fetch (fallback khi HTTP 475).
    """
    if prefer_playwright:
        return _login_via_playwright(session, plain)
    return post_login_encrypted(
        session,
        LOGIN_PATH,
        plain,
        http=http,
        force_cffi=cf_retry > 0 or _login_use_cffi(),
    )


def post_login_encrypted(
    session: dict,
    path: str,
    plain: dict,
    *,
    http: requests.Session | None = None,
    force_cffi: bool = False,
) -> tuple[int, Any, dict]:
    """
    POST login encrypt — ưu tiên curl_cffi (TLS Chrome) để tránh CF HTTP 475.
    force_cffi=True: chỉ curl_cffi (sau refresh CF).
    """
    from xoso66_deposit import (
        DEFAULT_UA,
        build_request_headers,
        crypto_available,
        encrypt_deposit_body,
        get_form_token,
    )
    from xoso66_proxy import build_proxies, ensure_proxy
    from xoso66_secure_headers import generate_secure_headers, pack_v2_ciphertext

    if not crypto_available():
        raise RuntimeError("pip install pycryptodome")

    form_token = get_form_token(session)
    encrypted_body, cek_k, aes_key = encrypt_deposit_body(session, plain)
    headers = build_request_headers(session, cek_k=cek_k, form_token=form_token)
    url = f"{resolve_base_url(session)}{path}"
    headers.update(
        generate_secure_headers(
            url,
            str(session.get("user_agent") or DEFAULT_UA),
        )
    )

    if force_cffi or _login_use_cffi():
        try:
            from curl_cffi import requests as cffi_requests

            ensure_proxy(session)
            r = cffi_requests.post(
                url,
                data=pack_v2_ciphertext(encrypted_body),
                headers=headers,
                cookies=session.get("cookies") or {},
                proxies=build_proxies(session["proxy"]),
                impersonate=os.environ.get("XOSO66_CF_IMPERSONATE", "chrome120"),
                timeout=45,
            )
            decrypted = _parse_encrypted_post_response(
                session,
                status=r.status_code,
                text=r.text or "",
                aes_key=aes_key,
                resp_headers=dict(r.headers),
                req_headers=headers,
            )
            _merge_any_response_cookies(session, r)
            return r.status_code, decrypted, dict(r.headers)
        except ImportError:
            if force_cffi:
                raise RuntimeError("curl_cffi chưa cài — pip install curl_cffi")
        except Exception:
            if force_cffi:
                raise

    return post_encrypted(session, path, plain, http=http)


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
        r = http.get(f"{resolve_base_url(session)}{ENCRYPT_KEY_PATH}", headers=headers, timeout=25)
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
        DEFAULT_UA,
        build_request_headers,
        crypto_available,
        decrypt_deposit_body,
        encrypt_deposit_body,
        get_form_token,
    )
    from xoso66_secure_headers import (
        CRYPTO_VERSION_HEADER,
        CRYPTO_VERSION_V2,
        generate_secure_headers,
        pack_v2_ciphertext,
        unpack_v2_ciphertext,
    )

    if not crypto_available():
        raise RuntimeError("pip install pycryptodome")

    form_token = get_form_token(session)
    encrypted_body, cek_k, aes_key = encrypt_deposit_body(session, plain)
    headers = build_request_headers(session, cek_k=cek_k, form_token=form_token)
    url = f"{resolve_base_url(session)}{path}"
    headers.update(
        generate_secure_headers(
            url,
            str(session.get("user_agent") or DEFAULT_UA),
        )
    )
    http = http or _requests_session(session)
    r = http.post(
        url,
        data=pack_v2_ciphertext(encrypted_body),
        headers=headers,
        timeout=45,
    )
    text = r.text
    if text.startswith('"') and text.endswith('"'):
        try:
            text = json.loads(text)
        except json.JSONDecodeError:
            text = text[1:-1]
    decrypted: Any = None
    if r.status_code == 200 and text:
        try:
            if text.lstrip().startswith(("{", "[")):
                # Lỗi validation có thể được server trả JSON rõ dù request dùng v2.
                decrypted = json.loads(text)
            else:
                response_version = (
                    r.headers.get(CRYPTO_VERSION_HEADER)
                    or r.headers.get(CRYPTO_VERSION_HEADER.title())
                    or headers.get(CRYPTO_VERSION_HEADER)
                )
                if response_version == CRYPTO_VERSION_V2:
                    text = unpack_v2_ciphertext(text)
                decrypted = decrypt_deposit_body(session, text, aes_key, dict(r.headers))
        except Exception as e:
            decrypted = {"_decrypt_error": str(e), "_cipher_preview": text[:200]}
    _merge_response_cookies(session, r)
    return r.status_code, decrypted, dict(r.headers)


def refresh_cloudflare(session: dict, *, prefer_playwright: bool = False) -> dict[str, Any]:
    """Tự lấy cf-* headers (+ cf_clearance nếu có)."""
    from xoso66_cf import refresh_cloudflare as _refresh

    return _refresh(session, prefer_playwright=prefer_playwright)


def _json_dict_from_preview(preview: str) -> dict[str, Any] | None:
    m = re.search(r"\{.*\}", preview or "", re.DOTALL)
    if not m:
        return None
    try:
        parsed = json.loads(m.group())
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _login_fail_message(
    body: Any,
    *,
    http_status: int = 200,
    had_captcha: bool = False,
    transport: str = "",
    step: str = "",
) -> str:
    """Mô tả lỗi login — tránh chỉ «Login thất bại code=None» khi thiếu msg."""
    parts: list[str] = []
    if step:
        parts.append(step)
    if transport:
        parts.append(transport)
    if http_status not in (0, 200):
        parts.append(f"HTTP {http_status}")

    if not isinstance(body, dict):
        if body is None:
            parts.append("response trống / không giải mã được")
        else:
            parts.append(f"body={body!r}"[:220])
        if had_captcha:
            parts.append("đã gửi captcha")
        return " — ".join(parts) if parts else "Login thất bại"

    decrypt_err = body.get("_decrypt_error")
    if decrypt_err:
        parts.append(f"giải mã response: {decrypt_err}")
        preview = str(body.get("_cipher_preview") or "")
        pl = preview.lower()
        if "cloudflare" in pl or "<html" in pl or "<!doctype" in pl:
            parts.append("server trả HTML/CF (không phải JSON API)")
        elif preview:
            inner = _json_dict_from_preview(preview)
            if inner:
                im = str(inner.get("msg") or inner.get("message") or "").strip()
                ic = inner.get("code")
                if im:
                    parts.append(im)
                elif ic is not None:
                    parts.append(f"code={ic}")
            else:
                parts.append(f"preview={preview[:100]!r}")

    msg = str(body.get("msg") or body.get("message") or "").strip()
    code = body.get("code")
    if msg and msg not in parts:
        parts.append(msg)
    if code is not None and not decrypt_err and not any(str(code) in p for p in parts):
        if not msg:
            parts.append(f"code={code}")
    if decrypt_err and code is None:
        inner = _json_dict_from_preview(str(body.get("_cipher_preview") or ""))
        if inner:
            im = str(inner.get("msg") or inner.get("message") or "").strip()
            if im and im not in parts:
                parts.append(f"server: {im}")
            ic = inner.get("code")
            if ic is not None and not any(str(ic) in p for p in parts):
                parts.append(f"code={ic}")
    if not decrypt_err and not msg and code is None:
        keys = [k for k in body if not str(k).startswith("_")]
        if not keys and not body:
            parts.append("JSON rỗng")
        elif keys:
            snippet = json.dumps({k: body[k] for k in keys[:6]}, ensure_ascii=False)
            parts.append(f"fields {snippet[:180]}")

    if had_captcha:
        parts.append("đã gửi captcha")
    out = " — ".join(p for p in parts if p)
    return out or "Login thất bại (server không trả msg/code)"


def _log_login_fail(
    session: dict,
    username: str,
    body: Any,
    *,
    http_status: int = 200,
    had_captcha: bool = False,
    transport: str = "",
    step: str = "",
) -> str:
    from xoso66_proxy import proxy_log_label

    fail_msg = _login_fail_message(
        body,
        http_status=http_status,
        had_captcha=had_captcha,
        transport=transport,
        step=step,
    )
    proxy_str = str(session.get("proxy") or "").strip()
    px = proxy_log_label(proxy_str) if proxy_str else "no-proxy"
    print(
        f"[LOGIN] {username} thất bại ({px}): {fail_msg}",
        flush=True,
    )
    if isinstance(body, dict) and body:
        extra = {
            k: body[k]
            for k in body
            if not str(k).startswith("_") and k not in ("msg", "message", "code", "data")
        }
        if extra:
            print(
                f"[LOGIN] {username} body extra: "
                f"{json.dumps(extra, ensure_ascii=False)[:400]}",
                flush=True,
            )
    return fail_msg


def is_login_cf_blocked(status: int | None) -> bool:
    """True nếu HTTP status là Cloudflare chặn POST login (475/403)."""
    try:
        return int(status or 0) in LOGIN_CF_BLOCK_HTTP_STATUSES
    except (TypeError, ValueError):
        return False


def _recover_login_after_cf_block(session: dict) -> Any:
    """
    Renew CF + form-token sau HTTP 475.
    CF thường chặn POST /login lần 2 (captcha retry) — cần cf-* mới trước khi gửi lại.
    """
    refresh_cloudflare(session, prefer_playwright=True)
    http = _requests_session(session)
    bootstrap_prelogin(session, http=http)
    return http


def _prefetch_login_captcha(session: dict) -> str:
    """GET captcha + Capsolver trước POST login — tránh POST trống → 1011 → POST lại (dễ 475)."""
    from xoso66_captcha_solver import (
        captcha_base64_from_payload,
        captcha_enabled,
        solve_image_captcha_auto,
    )
    from xoso66_register import get_captcha

    if not captcha_enabled():
        return ""
    username = session.get("username") or session.get("phone") or "?"
    try:
        cap = get_captcha(session)
        b64 = captcha_base64_from_payload(cap.get("raw") or {})
        if not b64:
            return ""
        solved = solve_image_captcha_auto(b64)
        if not solved.get("ok"):
            print(
                f"[LOGIN] {username} captcha prefetch fail: {solved.get('error')}",
                flush=True,
            )
            return ""
        text = str(solved.get("text") or "").strip()
        if text:
            print(f"[LOGIN] {username} captcha prefetch: {text!r}", flush=True)
        return text
    except Exception as e:
        print(f"[LOGIN] {username} captcha prefetch lỗi: {e}", flush=True)
        return ""


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

    aid = _account_id_from_session(session)
    blocked = _login_blocked_remaining(aid) if aid else 0.0
    if blocked > 0:
        raise LoginBlockedError(
            f"Login backoff ~{int(blocked)}s (CF/spam) — bỏ qua",
            remaining_sec=blocked,
        )

    # Toàn process chỉ 1 login cùng lúc — WS pool 16 nick không spam CF.
    if not _LOGIN_GATE.acquire(timeout=120):
        raise LoginBlockedError(
            "Login gate timeout — quá nhiều acc đang login",
            remaining_sec=30,
        )
    try:
        return _login_account_locked(session, username=str(username), password=str(password))
    finally:
        _LOGIN_GATE.release()


def _login_account_locked(
    session: dict, *, username: str, password: str
) -> dict:
    """
    Login giống web:
      1) POST không captcha (ô captcha chưa hiện) → thường code 1011 + ảnh
      2) Capsolver giải → POST 1 lần có captcha
      3) Sai captcha tối đa thêm 1 lần (không refresh CF giữa chừng)
    HTTP 475 → đánh Lỗi + thử Chrome UI login (mặc định bật); fail → raise.
    """
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
        solve_image_captcha_auto,
    )

    aid = _account_id_from_session(session)

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

    # Giống web: tối đa 1 lần không captcha + 2 lần có captcha (sai 1 lần rồi gửi lại).
    max_with_captcha = 2 if captcha_enabled() else 0
    captcha_text = ""
    data: Any = None
    code: Any = None
    msg = ""
    status = 0
    last_transport = "HTTP"

    def _post(captcha: str, *, use_playwright: bool) -> tuple[int, Any]:
        nonlocal last_transport
        last_transport = "Playwright" if use_playwright else "HTTP"
        plain = prepare_login_payload(
            username,
            password,
            captcha=captcha,
            source=str(session.get("login_source") or ""),
        )
        if use_playwright and captcha:
            print(
                f"[LOGIN] {username} gửi captcha={captcha!r} (Playwright)",
                flush=True,
            )
        st, body, _ = _submit_login(
            session,
            plain,
            http=http,
            prefer_playwright=use_playwright,
            cf_retry=0,
        )
        return st, body

    def _ok_result(body: dict) -> dict:
        user_data = body.get("data") if isinstance(body.get("data"), dict) else {}
        session.pop("captcha", None)
        if aid:
            _clear_login_blocked(aid)
        return {
            "cookies": session.get("cookies"),
            "form_token": session.get("form_token"),
            "headers": session.get("headers"),
            "user_info": user_data,
            "login_raw": body,
        }

    def _solve_from_body(body: Any) -> str:
        b64 = captcha_base64_from_payload(body) if isinstance(body, dict) else ""
        if not b64:
            try:
                from xoso66_register import get_captcha

                cap = get_captcha(session)
                b64 = captcha_base64_from_payload(cap.get("raw") or {})
            except Exception:
                b64 = ""
        if not b64:
            return ""
        solved = solve_image_captcha_auto(b64)
        if not solved.get("ok"):
            print(
                f"[LOGIN] Captcha Capsolver fail: {solved.get('error')}",
                flush=True,
            )
            return ""
        text = str(solved.get("text") or "").strip()
        if text:
            session["captcha"] = text
            print(f"[LOGIN] {username} captcha: {text!r}", flush=True)
        return text

    # --- Bước 1: không captcha (web: bấm Đăng nhập khi chưa hiện ô) ---
    status, data = _post("", use_playwright=False)
    if is_login_cf_blocked(status):
        return _handle_login_cf_block(session, aid, status=int(status))
    if status not in (0, 200):
        fail_msg = _log_login_fail(
            session,
            username,
            data,
            http_status=status,
            transport=last_transport,
            step="bước 1 không captcha",
        )
        raise RuntimeError(fail_msg or f"Login HTTP {status}")
    if not isinstance(data, dict):
        fail_msg = _log_login_fail(
            session,
            username,
            data,
            http_status=status,
            transport=last_transport,
            step="bước 1 không captcha",
        )
        raise RuntimeError(fail_msg)
    code = data.get("code")
    msg = str(data.get("msg") or "")
    if code == LOGIN_CODE_2FA:
        raise RuntimeError("Tài khoản cần 2FA (code 80080)")
    if code == 1:
        return _ok_result(data)
    if _is_site_login_spam_msg(msg):
        _raise_login_blocked(
            aid, f"Site rate-limit login: {msg}", sec=LOGIN_SPAM_BACKOFF_SEC
        )

    if not is_wrong_captcha_response(code, msg) or not captcha_enabled():
        fail_msg = _log_login_fail(
            session,
            username,
            data,
            http_status=status,
            transport=last_transport,
            step="bước 1 không captcha",
        )
        if not _login_response_is_crypto_glitch(data):
            from xoso66_account_errors import maybe_mark_account_loi_from_session

            maybe_mark_account_loi_from_session(session, fail_msg, source="login")
        raise RuntimeError(fail_msg)

    # --- Bước 2+: có captcha (web: hiện ô → nhập → Đăng nhập) ---
    for cap_i in range(max_with_captcha):
        captcha_text = _solve_from_body(data)
        if not captcha_text:
            break

        # Ưu tiên HTTP (giống form web); 475 → Playwright 1 lần; vẫn 475 → Lỗi.
        status, data = _post(captcha_text, use_playwright=False)
        if is_login_cf_blocked(status):
            print(
                f"[LOGIN] {username} HTTP {status} khi gửi captcha — thử Playwright 1 lần",
                flush=True,
            )
            status, data = _post(captcha_text, use_playwright=True)
        if is_login_cf_blocked(status):
            return _handle_login_cf_block(session, aid, status=int(status))
        if status not in (0, 200):
            fail_msg = _log_login_fail(
                session,
                username,
                data,
                http_status=status,
                had_captcha=bool(captcha_text),
                transport=last_transport,
                step="gửi captcha",
            )
            raise RuntimeError(fail_msg or f"Login HTTP {status}")
        if not isinstance(data, dict):
            fail_msg = _log_login_fail(
                session,
                username,
                data,
                http_status=status,
                had_captcha=bool(captcha_text),
                transport=last_transport,
                step="gửi captcha",
            )
            raise RuntimeError(fail_msg)
        code = data.get("code")
        msg = str(data.get("msg") or "")
        if code == LOGIN_CODE_2FA:
            raise RuntimeError("Tài khoản cần 2FA (code 80080)")
        if code == 1:
            return _ok_result(data)
        if _is_site_login_spam_msg(msg):
            _raise_login_blocked(
                aid, f"Site rate-limit login: {msg}", sec=LOGIN_SPAM_BACKOFF_SEC
            )
        if not is_wrong_captcha_response(code, msg):
            break
        if cap_i + 1 < max_with_captcha:
            print(
                f"[LOGIN] {username} captcha sai — lấy ảnh mới ({cap_i + 2}/{max_with_captcha})",
                flush=True,
            )

    fail_msg = _log_login_fail(
        session,
        username,
        data,
        http_status=status,
        had_captcha=bool(captcha_text),
        transport=last_transport,
        step="sau captcha",
    )
    if _is_site_login_spam_msg(fail_msg):
        _raise_login_blocked(
            aid, f"Site rate-limit login: {fail_msg}", sec=LOGIN_SPAM_BACKOFF_SEC
        )
    if not _login_response_is_crypto_glitch(data):
        from xoso66_account_errors import maybe_mark_account_loi_from_session

        maybe_mark_account_loi_from_session(session, fail_msg, source="login")
    raise RuntimeError(fail_msg)


def get_user_balance(session: dict, *, refresh: bool = True) -> dict[str, Any]:
    """GET /server/user/getBalance — probe session + số dư."""
    from xoso66_cf import is_cloudflare_rate_limited, mark_cf_rate_limited
    from xoso66_deposit import apply_response_tokens, build_common_headers, get_form_token

    if not str(session.get("form_token") or "").strip():
        if (session.get("cookies") or {}).get("PHPSESSID"):
            try:
                bootstrap_prelogin(session)
            except Exception:
                pass
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
        f"{resolve_base_url(session)}{GET_BALANCE_PATH}",
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

    # Cho phép đẩy balance↑ từ getBalance (persist thường thì không).
    session["_force_balance_sync"] = True
    save_session_runtime(aid, session)
    return {
        "ok": True,
        "account_id": aid,
        "balance": balance_f,
        "relogin_verified": bool(force_relogin),
    }


def session_is_valid(session: dict) -> bool:
    return bool(get_user_balance(session).get("ok"))


def prep_site_session_before_ws(
    account_id: str, *, force_balance_refresh: bool = True
) -> bool:
    """
    Probe/tool: ensure session + (optional) getBalance trước mở WS.
    Không đổi status — Hết Tiền do việc1 / _evict_het_tien_now.
    """
    aid = str(account_id or "").strip()
    if not aid:
        return False
    try:
        from xoso66_accounts_db import get_account, username_for_log
        from xoso66_config_util import load_config
        from xoso66_ws_pool import account_balance_vnd, min_balance_for_ws

        row = get_account(aid) or {}
        min_bal = float(min_balance_for_ws(load_config()))
        db_bal = float(account_balance_vnd(row))

        session = ensure_session(aid, force_login=False)
        if not force_balance_refresh and db_bal >= min_bal:
            return True

        rep = refresh_account_balance_to_db(aid, session, refresh=True)
        if not rep.get("ok"):
            if not force_balance_refresh and db_bal >= min_bal:
                return True
            return False
        bal = _as_money(rep.get("balance"))
        if bal is not None and bal < min_bal:
            print(
                f"[WS-POOL] {username_for_log(aid)}: sau check số dư "
                f"{bal:,.0f} < {min_bal:,.0f} — không mở WS",
                flush=True,
            )
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
      1) Đọc cookie từ chrome_profiles_data (Documents/, cạnh CMS) hoặc chờ CF trong Chrome đang mở
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

    # TTL local hết (>6h): probe getBalance trước — site có thể còn session, tránh login dồn → 475.
    if (
        not force_login
        and needs_relogin
        and not ignore_session_ttl
        and has_token
    ):
        bal_ttl = get_user_balance(acc)
        if bal_ttl.get("rate_limited"):
            raise CfRateLimitError(
                cf_rate_limit_message(acc),
                remaining_sec=cf_rate_limit_remaining(acc),
            )
        if bal_ttl.get("ok"):
            _mark_session_logged_in(acc)
            persist_session(account_id, acc)
            return acc
        force_login = True
    elif not force_login and needs_relogin:
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
        # CF 475 / site spam backoff — luôn tôn trọng (kể cả TTL force_login).
        blocked_rem = _login_blocked_remaining(account_id)
        if blocked_rem > 0:
            bal = get_user_balance(acc)
            if bal.get("ok"):
                _mark_session_logged_in(acc)
                return
            raise LoginBlockedError(
                f"Login backoff ~{int(blocked_rem)}s (CF/spam) — getBalance vẫn fail",
                remaining_sec=blocked_rem,
            )
        cooldown_rem = _login_cooldown_remaining(account_id)
        # force_login=True từ CLI: bỏ cooldown thường; TTL force vẫn giữ cooldown.
        bypass_cooldown = force_login and not needs_relogin
        if cooldown_rem > 0 and not bypass_cooldown:
            bal = get_user_balance(acc)
            if bal.get("ok"):
                _mark_session_logged_in(acc)
                return
            raise RuntimeError(
                f"Login cooldown ~{int(cooldown_rem)}s — getBalance vẫn fail"
            )
        _mark_login_attempt(account_id)
        apply_session_merge(acc, login_account(acc))
        _mark_session_logged_in(acc)
        _clear_login_blocked(account_id)

    try:
        _try_login()
    except (CfRateLimitError, LoginBlockedError):
        raise
    except Exception as login_err:
        err_s = str(login_err)
        # 475 đã đánh Lỗi trong login_account — không refresh+login lại.
        if "475" in err_s or is_login_cf_blocked(getattr(login_err, "status", None)):
            if "đã đánh Lỗi" not in err_s:
                _mark_account_loi_http_475(acc, account_id, status=475)
            raise
        if _is_site_login_spam_msg(err_s):
            if _login_blocked_remaining(account_id) <= 0:
                _mark_login_blocked(account_id, sec=LOGIN_SPAM_BACKOFF_SEC)
            raise
        if not is_cf_rate_limited(acc):
            refresh_cloudflare(acc, prefer_playwright=True)
        _try_login()

    fresh = load_sessions().get(account_id) or {}
    if fresh:
        apply_session_merge(acc, fresh)
    try:
        bootstrap_prelogin(acc)
    except Exception:
        pass

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
