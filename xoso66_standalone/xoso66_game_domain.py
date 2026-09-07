# -*- coding: utf-8 -*-
"""
Per-account game site domain.

- Chưa gắn base_url → default từ config / env.
- Đã gắn → mọi HTTP/CF/Playwright site dùng domain đó.
- Auto-rotate chỉ khi proxy SOCKS OK nhưng không kết nối được domain game hiện tại.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any
from urllib.parse import urlparse

_FALLBACK_DEFAULT = "https://hnwp57e0.whskxk5.com"

_ROTATE_HISTORY: dict[str, list[float]] = {}
_ROTATE_COOLDOWN_UNTIL: dict[str, float] = {}
_ROTATE_LOCK = threading.Lock()


def normalize_base_url(url: str | None) -> str:
    raw = str(url or "").strip().rstrip("/")
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    p = urlparse(raw)
    if not p.scheme or not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}".rstrip("/")


def _game_domains_cfg() -> dict[str, Any]:
    try:
        from xoso66_config_util import load_config

        cfg = load_config()
        gd = cfg.get("game_domains")
        return gd if isinstance(gd, dict) else {}
    except Exception:
        return {}


def default_base_url() -> str:
    env = (os.environ.get("XOSO66_BASE_URL") or "").strip()
    if env:
        return normalize_base_url(env) or _FALLBACK_DEFAULT
    gd = _game_domains_cfg()
    cfg_url = normalize_base_url(str(gd.get("default_base_url") or ""))
    return cfg_url or _FALLBACK_DEFAULT


def candidate_urls() -> list[str]:
    """Danh sách domain theo đúng thứ tự config (1→2→3…)."""
    gd = _game_domains_cfg()
    raw = gd.get("candidates")
    out: list[str] = []
    seen: set[str] = set()
    if isinstance(raw, list):
        for item in raw:
            u = normalize_base_url(str(item or ""))
            if u and u not in seen:
                seen.add(u)
                out.append(u)
    # Default chỉ bổ sung cuối nếu chưa có — không chen đầu (giữ thứ tự 1..N).
    d = default_base_url()
    if d and d not in seen:
        out.append(d)
    return out


def resolve_base_url(session: dict | None = None) -> str:
    """base_url hiệu lực cho session/account (rỗng → default)."""
    if session:
        assigned = normalize_base_url(str(session.get("base_url") or ""))
        if assigned:
            return assigned
    return default_base_url()


def site_host(session: dict | None = None, *, base_url: str | None = None) -> str:
    url = normalize_base_url(base_url) if base_url else resolve_base_url(session)
    return urlparse(url).netloc or "localhost"


def probe_domain_via_proxy(
    proxy: str,
    base_url: str,
    *,
    timeout: float | None = None,
) -> tuple[bool, str]:
    """GET nhẹ tới site qua SOCKS. OK nếu TCP+HTTP trả về (kể cả 403 CF)."""
    from xoso66_proxy import build_proxies, has_proxy

    url = normalize_base_url(base_url)
    if not url:
        return False, "thiếu base_url"
    gd = _game_domains_cfg()
    wait = float(timeout if timeout is not None else gd.get("probe_timeout_sec") or 5)
    wait = max(1.0, wait)
    probe_url = f"{url}/server/index/encryptKey"
    try:
        import requests

        proxies = build_proxies(proxy) if has_proxy(proxy) else {}
        r = requests.get(
            probe_url,
            proxies=proxies or None,
            timeout=wait,
            headers={
                "user-agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "accept": "application/json, text/plain, */*",
            },
            allow_redirects=True,
        )
        return True, f"http {r.status_code}"
    except Exception as e:
        return False, str(e).strip()[:160]


def next_domain_in_order(current: str | None) -> list[str]:
    """Candidates theo vòng: sau current → … → cuối → đầu → … (bỏ current)."""
    cands = candidate_urls()
    if not cands:
        return []
    cur = normalize_base_url(current)
    try:
        idx = cands.index(cur) if cur else -1
    except ValueError:
        idx = -1
    n = len(cands)
    ordered: list[str] = []
    for offset in range(1, n + 1):
        cand = cands[(idx + offset) % n]
        if cand != cur:
            ordered.append(cand)
    return ordered


def pick_reachable_domain(
    session: dict,
    *,
    exclude: str | None = None,
) -> str | None:
    """
    Đổi theo thứ tự candidates: đang #1 → thử #2, rồi #3… (wrap về đầu).
    Bỏ qua candidate probe không được qua proxy.
    """
    from xoso66_proxy import resolve_proxy

    proxy = resolve_proxy(session)
    current = normalize_base_url(exclude) or resolve_base_url(session)
    gd = _game_domains_cfg()
    timeout = float(gd.get("probe_timeout_sec") or 5)
    for cand in next_domain_in_order(current):
        ok, _msg = probe_domain_via_proxy(proxy, cand, timeout=timeout)
        if ok:
            return cand
    return None


def assign_account_base_url(account_id: str, url: str) -> dict[str, Any]:
    """Ghi accounts.base_url ('' = dùng default)."""
    from xoso66_accounts_db import update_account

    aid = str(account_id or "").strip()
    if not aid:
        raise ValueError("thiếu account_id")
    raw = str(url or "").strip()
    if not raw:
        return update_account(aid, {"base_url": ""})
    normalized = normalize_base_url(raw)
    if not normalized:
        raise ValueError(f"base_url không hợp lệ: {url!r}")
    return update_account(aid, {"base_url": normalized})


def clear_site_session_after_domain_switch(session: dict) -> None:
    """Xóa cookies/headers CF site + form_token; giữ password/proxy/base_url."""
    session["cookies"] = {}
    session["headers"] = {}
    for k in (
        "form_token",
        "cek_p",
        "aes_session_key",
        "cf_auth_token",
        "cf_con_s",
        "cf_pass",
        "c_a_i",
        "session_login_at",
        "balance_verified_at",
        "balance_verified_money",
        "login_raw",
        "user_info",
        "ukey",
    ):
        session.pop(k, None)
    mg = session.get("minigame")
    if isinstance(mg, dict):
        for k in (
            "user_token",
            "user_token_at",
            "ws_token",
            "ws_token_issued_at",
            "ws_url",
            "cookies",
            "last_game_id",
        ):
            mg.pop(k, None)
        session["minigame"] = mg


def set_account_domain(
    account_id: str,
    session: dict,
    base_url: str,
    *,
    relogin: bool = True,
) -> dict[str, Any]:
    """
    Gắn domain tay từ CMS: ghi base_url → clear session site → login lại lấy token.
    base_url rỗng = về mặc định config.
    """
    aid = str(account_id or session.get("id") or "").strip()
    user = str(session.get("username") or aid or "?")
    old = resolve_base_url(session)
    raw = str(base_url or "").strip()
    if raw:
        new_stored = normalize_base_url(raw)
        if not new_stored:
            raise ValueError(f"base_url không hợp lệ: {base_url!r}")
    else:
        new_stored = ""
    new_effective = new_stored or default_base_url()

    assign_account_base_url(aid, new_stored)
    session["base_url"] = new_stored
    clear_site_session_after_domain_switch(session)

    try:
        from xoso66_accounts_db import save_session_runtime

        save_session_runtime(aid, session)
    except Exception:
        try:
            from xoso66_session import persist_session

            persist_session(aid, session)
        except Exception:
            pass

    print(
        f"[DOMAIN] {user}: set {old} → {new_effective}"
        f"{'' if new_stored else ' (mặc định)'}",
        flush=True,
    )

    login_ok = None
    login_msg = ""
    token_ok = None
    token_msg = ""
    if relogin:
        try:
            from xoso66_session import ensure_session

            ensure_session(aid, force_login=True)
            login_ok = True
            login_msg = "relogin_ok"
        except Exception as e:
            login_ok = False
            login_msg = str(e).strip()[:200]
            print(f"[DOMAIN] {user}: relogin fail: {login_msg}", flush=True)

        if login_ok:
            try:
                from xoso66_minigame_refresh import refresh_user_token_via_gameurl
                from xoso66_sessions_io import load_sessions

                sess2 = load_sessions().get(aid) or session
                rep = refresh_user_token_via_gameurl(sess2)
                token_ok = bool(rep.get("ok"))
                token_msg = str(
                    rep.get("error") or rep.get("msg") or rep.get("method") or ""
                )[:160]
                if token_ok:
                    try:
                        from xoso66_session import persist_session

                        persist_session(aid, sess2)
                    except Exception:
                        pass
            except Exception as e:
                token_ok = False
                token_msg = str(e).strip()[:200]

    return {
        "ok": True if login_ok is not False else False,
        "account_id": aid,
        "username": user,
        "old": old,
        "new": new_effective,
        "base_url": new_stored,
        "login_ok": login_ok,
        "login_msg": login_msg,
        "token_ok": token_ok,
        "token_msg": token_msg,
    }


def _rotate_enabled() -> bool:
    return bool(_game_domains_cfg().get("auto_rotate_enabled", True))


def _cooldown_sec() -> float:
    return max(0.0, float(_game_domains_cfg().get("rotate_cooldown_sec") or 600))


def _max_rotates_per_hour() -> int:
    return max(0, int(_game_domains_cfg().get("max_rotates_per_hour") or 3))


def _can_rotate(account_id: str) -> tuple[bool, str]:
    if not _rotate_enabled():
        return False, "auto_rotate_disabled"
    now = time.time()
    with _ROTATE_LOCK:
        until = _ROTATE_COOLDOWN_UNTIL.get(account_id, 0.0)
        if now < until:
            return False, f"cooldown_{int(until - now)}s"
        hist = [t for t in _ROTATE_HISTORY.get(account_id, []) if now - t < 3600]
        _ROTATE_HISTORY[account_id] = hist
        max_n = _max_rotates_per_hour()
        if max_n and len(hist) >= max_n:
            return False, "max_rotates_per_hour"
    return True, ""


def _mark_rotated(account_id: str) -> None:
    now = time.time()
    with _ROTATE_LOCK:
        hist = [t for t in _ROTATE_HISTORY.get(account_id, []) if now - t < 3600]
        hist.append(now)
        _ROTATE_HISTORY[account_id] = hist
        _ROTATE_COOLDOWN_UNTIL[account_id] = now + _cooldown_sec()


def maybe_rotate_domain(
    account_id: str,
    session: dict,
    reason: str,
    *,
    force: bool = False,
    relogin: bool = True,
) -> dict[str, Any]:
    """
    Chỉ đổi domain khi: proxy SOCKS OK nhưng không kết nối được tới domain game hiện tại.
    force=True (API tay) bỏ qua bước chứng minh domain hiện tại chết.
    """
    from xoso66_proxy import probe_proxy_socks, resolve_proxy

    aid = str(account_id or session.get("id") or "").strip()
    user = str(session.get("username") or aid or "?")
    old = resolve_base_url(session)

    if not force:
        ok_gate, gate_msg = _can_rotate(aid)
        if not ok_gate:
            print(f"[DOMAIN] {user}: bỏ rotate ({gate_msg}) reason={reason}", flush=True)
            return {
                "ok": False,
                "rotated": False,
                "old": old,
                "new": old,
                "message": gate_msg,
            }

    proxy = resolve_proxy(session)
    socks_ok, socks_err = probe_proxy_socks(proxy)
    if not socks_ok:
        print(
            f"[DOMAIN] {user}: bỏ rotate (proxy_dead) reason={reason} | {socks_err}",
            flush=True,
        )
        return {
            "ok": False,
            "rotated": False,
            "old": old,
            "new": old,
            "message": f"proxy_dead:{socks_err}",
        }

    # Proxy sống: chỉ rotate nếu domain hiện tại không probe được (trừ force tay).
    if not force:
        gd = _game_domains_cfg()
        timeout = float(gd.get("probe_timeout_sec") or 5)
        cur_ok, cur_msg = probe_domain_via_proxy(proxy, old, timeout=timeout)
        if cur_ok:
            print(
                f"[DOMAIN] {user}: bỏ rotate (domain vẫn vào được {old} — {cur_msg}) "
                f"reason={reason}",
                flush=True,
            )
            return {
                "ok": False,
                "rotated": False,
                "old": old,
                "new": old,
                "message": f"skip_game_reachable:{cur_msg}",
            }

    new_url = pick_reachable_domain(session, exclude=old)
    if not new_url:
        print(
            f"[DOMAIN] {user}: bỏ rotate (không còn candidate sống) "
            f"old={old} reason={reason}",
            flush=True,
        )
        return {
            "ok": False,
            "rotated": False,
            "old": old,
            "new": old,
            "message": "no_reachable_candidate",
        }

    try:
        assign_account_base_url(aid, new_url)
    except Exception as e:
        return {
            "ok": False,
            "rotated": False,
            "old": old,
            "new": old,
            "message": f"assign_fail:{e}",
        }

    session["base_url"] = new_url
    clear_site_session_after_domain_switch(session)

    try:
        from xoso66_accounts_db import save_session_runtime

        save_session_runtime(aid, session)
    except Exception:
        try:
            from xoso66_session import persist_session

            persist_session(aid, session)
        except Exception:
            pass

    _mark_rotated(aid)
    print(f"[DOMAIN] {user}: {old} → {new_url} (reason={reason})", flush=True)

    login_ok = None
    login_msg = ""
    if relogin:
        try:
            from xoso66_session import ensure_session

            ensure_session(aid, force_login=True)
            login_ok = True
            login_msg = "relogin_ok"
        except Exception as e:
            login_ok = False
            login_msg = str(e).strip()[:200]
            print(f"[DOMAIN] {user}: relogin sau rotate fail: {login_msg}", flush=True)

    return {
        "ok": True,
        "rotated": True,
        "old": old,
        "new": new_url,
        "message": f"rotated:{reason}",
        "login_ok": login_ok,
        "login_msg": login_msg,
    }


# Legacy import-time default. Prefer resolve_base_url(session).
BASE_URL = default_base_url()
