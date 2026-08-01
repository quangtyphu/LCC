# -*- coding: utf-8 -*-
"""
Proxy SOCKS5 cho mọi traffic XOSO66 (giống LC79).

Format: host:port:user:pass  (vd. 118.70.171.104:20023:PogCLP:wSMZkU)

Cấu hình mặc định (theo thứ tự):
  1. session["proxy"]
  2. env XOSO66_DEFAULT_PROXY
  3. xoso66_config_util.DEFAULT_PROXY (hardcode)

Lỗi SOCKS/proxy trên HTTP:
  - Mọi _requests_session / _game_http / minigame HTTP đi qua ProxyAwareSession
  - Retry ngay (XOSO66_PROXY_HTTP_RETRY, mặc định 2) rồi report_proxy_dead
  - Sau XOSO66_PROXY_FAIL_MAX_ATTEMPTS (3) → status «Lỗi proxy» (CMS/DB)
"""

from __future__ import annotations

import os
import socket
import threading
import time
from typing import Any
from urllib.parse import urlparse

import socks

_PROXY_DEAD_UNTIL: dict[str, float] = {}
_PROXY_DEAD_LOCK = threading.Lock()
_PROXY_LAST_INCR_AT: dict[str, float] = {}
PROXY_DEAD_COOLDOWN_SEC = float(os.environ.get("XOSO66_PROXY_DEAD_COOLDOWN_SEC", "600"))
PROXY_PROBE_TIMEOUT_SEC = float(os.environ.get("XOSO66_PROXY_PROBE_TIMEOUT_SEC", "8"))
PROXY_FAIL_MAX_ATTEMPTS = int(os.environ.get("XOSO66_PROXY_FAIL_MAX_ATTEMPTS", "3"))
# Debounce fail_count khi nhiều lớp (HTTP wrap + caller) báo cùng một lần chết.
PROXY_FAIL_DEBOUNCE_SEC = float(os.environ.get("XOSO66_PROXY_FAIL_DEBOUNCE_SEC", "20"))
# Số lần retry ngay trên cùng request khi lỗi SOCKS (0 = không retry, chỉ báo).
PROXY_HTTP_RETRY = int(os.environ.get("XOSO66_PROXY_HTTP_RETRY", "2"))


class ProxyRequiredError(ValueError):
    """Thiếu proxy — mọi API game bắt buộc đi qua SOCKS5."""


def has_proxy(proxy_str: str | None) -> bool:
    return bool(str(proxy_str or "").strip())


def parse_proxy(proxy_str: str) -> tuple[str, int, str, str]:
    """host:port:user:pass hoặc host:port."""
    s = (proxy_str or "").strip()
    if not s:
        raise ValueError("proxy rỗng")
    parts = s.split(":")
    if len(parts) == 4:
        host, port_s, user, pwd = parts
        return host.strip(), int(port_s), user.strip(), pwd.strip()
    if len(parts) == 2:
        return parts[0].strip(), int(parts[1]), "", ""
    raise ValueError(
        'proxy phải dạng "host:port:user:pass" hoặc "host:port", '
        f"nhận: {proxy_str!r}"
    )


def load_default_proxy() -> str:
    from xoso66_config_util import hardcoded_default_proxy

    return hardcoded_default_proxy()


def resolve_proxy(session: dict | None) -> str:
    """Proxy cho request: session trước, rồi default."""
    if session:
        p = (session.get("proxy") or "").strip()
        if p:
            return p
    return load_default_proxy()


def require_explicit_proxy(proxy_str: str | None) -> str:
    """
    Proxy bắt buộc do caller truyền (đăng ký / CMS provision).
    Không fallback default_proxy từ env/config.
    """
    p = (proxy_str or "").strip()
    if not p:
        raise ProxyRequiredError(
            "Thiếu proxy bắt buộc. Truyền proxy dạng host:port:user:pass "
            '(vd. "118.70.171.104:20023:user:pass"). '
            "Đăng ký không dùng default_proxy trong config."
        )
    parse_proxy(p)
    return p


def ensure_proxy(session: dict, *, explicit_only: bool = False) -> str:
    """
    Gắn proxy vào session.
    explicit_only=True: chỉ session['proxy'], không đọc default (đăng ký).
    """
    if explicit_only:
        p = require_explicit_proxy(session.get("proxy"))
    else:
        p = resolve_proxy(session)
        if not p:
            raise ProxyRequiredError(
                "Thiếu proxy. Thêm session['proxy'] hoặc "
                "XOSO66_DEFAULT_PROXY hoặc DEFAULT_PROXY trong xoso66_config_util.py "
                '(vd. "118.70.171.104:20023:user:pass").'
            )
    session["proxy"] = p
    return p


def build_proxies(proxy_str: str) -> dict[str, str]:
    """Dict cho requests / curl_cffi — socks5h như LC79."""
    if not has_proxy(proxy_str):
        return {}
    host, port, user, pwd = parse_proxy(proxy_str)
    if user:
        auth = f"{user}:{pwd}@"
    else:
        auth = ""
    url = f"socks5h://{auth}{host}:{port}"
    return {"http": url, "https": url}


def proxy_has_auth(proxy_str: str) -> bool:
    """True nếu proxy dạng host:port:user:pass."""
    try:
        _, _, user, _ = parse_proxy(proxy_str)
        return bool(user)
    except ValueError:
        return False


def playwright_proxy(proxy_str: str) -> dict[str, str] | None:
    """Dict proxy cho Playwright (relay HTTP local nếu SOCKS5 có user/pass)."""
    if not has_proxy(proxy_str):
        return None
    from xoso66_proxy_relay import playwright_proxy_with_relay

    return playwright_proxy_with_relay(proxy_str)


def apply_requests_proxy(req_session: Any, proxy_str: str) -> None:
    px = build_proxies(proxy_str)
    if px:
        req_session.proxies.update(px)


def proxy_log_label(proxy_str: str) -> str:
    """host:port cho log (không in password)."""
    try:
        host, port, user, _ = parse_proxy(proxy_str)
        if user:
            return f"{host}:{port} (socks5, user {user})"
        return f"{host}:{port} (socks5)"
    except Exception:
        s = (proxy_str or "").strip()
        return s[:48] + ("…" if len(s) > 48 else "") if s else "(không có proxy)"


def proxy_source_label(session: dict | None, *, account_proxy: str = "") -> str:
    """
    Mô tả proxy đang dùng: riêng acc hay default config.
    Gọi sau ensure_proxy(session).
    """
    used = str((session or {}).get("proxy") or "").strip()
    acc = str(account_proxy or "").strip()
    default = load_default_proxy()
    if acc:
        return f"proxy acc → {proxy_log_label(used)}"
    if used and default and used == default:
        return f"proxy default config → {proxy_log_label(used)}"
    if used:
        return f"proxy session → {proxy_log_label(used)}"
    return "proxy: (chưa gán)"


def site_host(base_url: str) -> str:
    return urlparse(base_url.rstrip("/")).netloc or "localhost"


_PROXY_ERROR_MARKERS = (
    "proxy",
    "socks",
    "connecttimeout",
    "timed out",
    "timeout",
    "connection refused",
    "connection closed unexpectedly",
    "generalproxyerror",
    "socks connect",
    "max retries exceeded",
)


def is_proxy_error_message(msg: str | None) -> bool:
    """True nếu chuỗi lỗi trông như proxy/SOCKS chết (vd. SOCKSHTTPSConnectionPool)."""
    err = str(msg or "").lower()
    return bool(err) and any(m in err for m in _PROXY_ERROR_MARKERS)


def is_proxy_transport_error(exc: BaseException | None) -> bool:
    """True nếu lỗi do proxy/SOCKS không kết nối được (không phải CF/site)."""
    if exc is None:
        return False
    if isinstance(exc, (TimeoutError, ConnectionError, socket.timeout)):
        return True
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in (
        10060,
        10061,
        110,
        111,
    ):
        return True
    try:
        import requests

        if isinstance(
            exc,
            (
                requests.exceptions.ConnectTimeout,
                requests.exceptions.ProxyError,
                requests.exceptions.ConnectionError,
            ),
        ):
            return True
    except Exception:
        pass
    try:
        from socks import GeneralProxyError

        if isinstance(exc, GeneralProxyError):
            return True
    except Exception:
        pass
    if is_proxy_error_message(str(exc)):
        return True
    cause = exc.__cause__
    if cause is not None and cause is not exc:
        return is_proxy_transport_error(cause)
    return False


def is_proxy_dead(account_id: str) -> bool:
    """Acc đang cooldown hoặc đã status «Lỗi proxy» — không bù WS."""
    aid = str(account_id or "").strip()
    if not aid:
        return False
    try:
        from xoso66_accounts_db import STATUS_LOI_PROXY, get_account

        row = get_account(aid) or {}
        if str(row.get("status") or "").strip() == STATUS_LOI_PROXY:
            return True
    except Exception:
        pass
    with _PROXY_DEAD_LOCK:
        until = _PROXY_DEAD_UNTIL.get(aid, 0.0)
    return time.time() < until


def _proxy_fail_count(account_id: str) -> int:
    from xoso66_accounts_db import get_account

    row = get_account(account_id) or {}
    sj = row.get("session_json") or {}
    if not isinstance(sj, dict):
        return 0
    runtime = sj.get("_runtime") or {}
    if not isinstance(runtime, dict):
        return 0
    try:
        return max(0, int(runtime.get("proxy_fail_count") or 0))
    except (TypeError, ValueError):
        return 0


def _set_proxy_fail_count(account_id: str, count: int) -> int:
    from xoso66_accounts_db import get_account, update_account

    aid = str(account_id or "").strip()
    if not aid:
        return 0
    row = get_account(aid) or {}
    sj = dict(row.get("session_json") or {}) if isinstance(row.get("session_json"), dict) else {}
    runtime = dict(sj.get("_runtime") or {}) if isinstance(sj.get("_runtime"), dict) else {}
    runtime["proxy_fail_count"] = max(0, int(count))
    sj["_runtime"] = runtime
    update_account(aid, {"session_json": sj})
    return int(runtime["proxy_fail_count"])


def _incr_proxy_fail_count(account_id: str) -> int:
    with _PROXY_DEAD_LOCK:
        n = _proxy_fail_count(account_id) + 1
        return _set_proxy_fail_count(account_id, n)


def _clear_proxy_fail_count(account_id: str) -> None:
    with _PROXY_DEAD_LOCK:
        if _proxy_fail_count(account_id) > 0:
            _set_proxy_fail_count(account_id, 0)


def mark_account_loi_proxy(account_id: str, *, reason: str = "") -> bool:
    """Sau nhiều lần proxy fail → status «Lỗi proxy», dừng WS/auto-mission."""
    from xoso66_accounts_db import (
        STATUS_LOI_PROXY,
        get_account,
        set_account_status,
        username_for_log,
    )

    aid = str(account_id or "").strip()
    if not aid:
        return False
    row = get_account(aid) or {}
    if str(row.get("status") or "").strip() == STATUS_LOI_PROXY:
        return True
    detail = str(reason or "proxy chết sau nhiều lần thử").strip()[:200]
    set_account_status(aid, STATUS_LOI_PROXY, reason=detail)
    with _PROXY_DEAD_LOCK:
        _PROXY_DEAD_UNTIL.pop(aid, None)
    _clear_proxy_fail_count(aid)
    try:
        from xoso66_auto_mission_reward import cancel_mission_claim_queue

        cancel_mission_claim_queue(aid)
    except Exception:
        pass
    try:
        from xoso66_ws_pool import clear_pending_ws_slot, request_ws_evict_and_resync

        clear_pending_ws_slot(aid)
        request_ws_evict_and_resync([aid])
    except Exception:
        pass
    print(
        f"[PROXY] {username_for_log(aid, row)}: đã chuyển → Lỗi proxy — {detail}",
        flush=True,
    )
    return True


def clear_proxy_dead(account_id: str) -> None:
    aid = str(account_id or "").strip()
    if not aid:
        return
    with _PROXY_DEAD_LOCK:
        _PROXY_DEAD_UNTIL.pop(aid, None)
        _PROXY_LAST_INCR_AT.pop(aid, None)
    _clear_proxy_fail_count(aid)


def report_proxy_dead(
    account_id: str,
    *,
    proxy_str: str = "",
    source: str = "",
    detail: str = "",
    exc: BaseException | None = None,
) -> None:
    """
    Báo proxy chết + cooldown — loại khỏi bù WS trong PROXY_DEAD_COOLDOWN_SEC.
    Sau PROXY_FAIL_MAX_ATTEMPTS lần (có debounce) → status «Lỗi proxy» (CMS).
    """
    aid = str(account_id or "").strip()
    if not aid:
        return
    try:
        from xoso66_accounts_db import STATUS_LOI_PROXY, get_account

        row = get_account(aid) or {}
        if str(row.get("status") or "").strip() == STATUS_LOI_PROXY:
            return
    except Exception:
        row = {}
    if not detail and exc is not None:
        detail = str(exc).strip()[:160]
    if not detail:
        detail = "không kết nối được qua proxy"

    if not proxy_str:
        try:
            from xoso66_accounts_db import get_account

            row = get_account(aid) or {}
            proxy_str = resolve_proxy(row)
        except Exception:
            proxy_str = ""

    from xoso66_accounts_db import username_for_log

    user = username_for_log(aid)
    label = proxy_log_label(proxy_str) if proxy_str else "(không có proxy)"
    cooldown = max(60.0, PROXY_DEAD_COOLDOWN_SEC)
    now = time.time()
    until = now + cooldown
    with _PROXY_DEAD_LOCK:
        prev = _PROXY_DEAD_UNTIL.get(aid, 0.0)
        _PROXY_DEAD_UNTIL[aid] = until
        should_log = now >= prev - 5.0
        last_incr = _PROXY_LAST_INCR_AT.get(aid, 0.0)
        do_incr = (now - last_incr) >= max(1.0, PROXY_FAIL_DEBOUNCE_SEC)
        if do_incr:
            _PROXY_LAST_INCR_AT[aid] = now

    if should_log:
        src = f" ({source})" if source else ""
        print(
            f"[PROXY] {user}: proxy chết — {label}{src} | {detail} "
            f"— bỏ WS {cooldown:.0f}s",
            flush=True,
        )

    if not do_incr:
        try:
            from xoso66_ws_pool import clear_pending_ws_slot

            clear_pending_ws_slot(aid)
        except Exception:
            pass
        return

    fail_n = _incr_proxy_fail_count(aid)
    if fail_n >= max(1, PROXY_FAIL_MAX_ATTEMPTS):
        mark_account_loi_proxy(
            aid,
            reason=f"{detail} ({fail_n}/{PROXY_FAIL_MAX_ATTEMPTS} lần)",
        )
        return

    try:
        from xoso66_ws_pool import clear_pending_ws_slot

        clear_pending_ws_slot(aid)
    except Exception:
        pass


def account_id_from_session(session: dict | None) -> str:
    """Lấy account_id từ session dict (id / _balance_log_account_id)."""
    if not isinstance(session, dict):
        return ""
    return str(
        session.get("id")
        or session.get("_balance_log_account_id")
        or session.get("account_id")
        or ""
    ).strip()


def notify_session_proxy_failure(
    session: dict | None,
    exc: BaseException,
    *,
    source: str = "HTTP",
) -> bool:
    """Báo proxy chết từ session dict + exception. Trả True nếu đã xử lý."""
    aid = account_id_from_session(session)
    if not aid:
        return False
    proxy_str = ""
    if isinstance(session, dict):
        proxy_str = str(session.get("proxy") or "")
    return maybe_report_proxy_dead_from_exception(
        aid, exc, proxy_str=proxy_str, source=source
    )


class ProxyAwareSession:
    """
    Bọc requests.Session: retry khi lỗi SOCKS/proxy, rồi báo chết + re-raise.
    Mọi _requests_session / minigame HTTP đi qua lớp này.
    """

    def __init__(
        self,
        http: Any,
        session: dict,
        *,
        source: str = "HTTP",
        retries: int | None = None,
    ) -> None:
        self._http = http
        self._session = session if isinstance(session, dict) else {}
        self._source = source or "HTTP"
        self._retries = (
            PROXY_HTTP_RETRY if retries is None else max(0, int(retries))
        )

    def request(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("request", *args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("get", *args, **kwargs)

    def post(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("post", *args, **kwargs)

    def put(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("put", *args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("delete", *args, **kwargs)

    def head(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("head", *args, **kwargs)

    def options(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("options", *args, **kwargs)

    def close(self) -> None:
        close = getattr(self._http, "close", None)
        if callable(close):
            close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._http, name)

    def _call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(self._http, method_name)
        last_exc: BaseException | None = None
        attempts = self._retries + 1
        for attempt in range(attempts):
            try:
                return fn(*args, **kwargs)
            except BaseException as e:
                last_exc = e
                if not is_proxy_transport_error(e):
                    raise
                if attempt + 1 < attempts:
                    time.sleep(min(1.5, 0.35 * (attempt + 1)))
                    continue
                notify_session_proxy_failure(
                    self._session, e, source=self._source
                )
                raise
        assert last_exc is not None
        raise last_exc


def wrap_requests_session(
    http: Any,
    session: dict,
    *,
    source: str = "HTTP",
    retries: int | None = None,
) -> ProxyAwareSession:
    """Bọc requests.Session đã gắn SOCKS — retry + báo Lỗi proxy."""
    if isinstance(http, ProxyAwareSession):
        return http
    return ProxyAwareSession(http, session, source=source, retries=retries)


def probe_proxy_socks(
    proxy_str: str, *, timeout: float | None = None
) -> tuple[bool, str]:
    """
    Thử SOCKS5 tới proxy (TCP + handshake). Trả (ok, lỗi).
    """
    p = str(proxy_str or "").strip()
    if not p:
        return False, "thiếu proxy"
    wait = PROXY_PROBE_TIMEOUT_SEC if timeout is None else max(1.0, float(timeout))
    try:
        host, port, user, pwd = parse_proxy(p)
        sock = socks.socksocket()
        sock.set_proxy(socks.SOCKS5, host, port, True, user, pwd)
        sock.settimeout(wait)
        sock.connect(("1.1.1.1", 80))
        sock.close()
        return True, ""
    except Exception as e:
        return False, str(e).strip()[:160]


def maybe_report_proxy_dead_from_exception(
    account_id: str,
    exc: BaseException,
    *,
    proxy_str: str = "",
    source: str = "",
) -> bool:
    """Nếu exc là lỗi proxy → báo + cooldown. Trả True nếu đã xử lý."""
    if not is_proxy_transport_error(exc):
        return False
    report_proxy_dead(
        account_id,
        proxy_str=proxy_str,
        source=source,
        exc=exc,
    )
    return True


def maybe_report_proxy_dead_from_message(
    account_id: str,
    msg: str | None,
    *,
    proxy_str: str = "",
    source: str = "",
) -> bool:
    """Nếu chuỗi lỗi là proxy/SOCKS → báo + cooldown. Trả True nếu đã xử lý."""
    detail = str(msg or "").strip()
    if not is_proxy_error_message(detail):
        return False
    report_proxy_dead(
        account_id,
        proxy_str=proxy_str,
        source=source,
        detail=detail[:160],
    )
    return True
