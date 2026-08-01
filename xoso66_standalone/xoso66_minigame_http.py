# -*- coding: utf-8 -*-
"""HTTP mini-game.vip — mọi request bắt buộc qua SOCKS5 (session proxy)."""

from __future__ import annotations

import os
from typing import Any

import requests

MINIGAME_BASE = os.environ.get(
    "XOSO66_MINIGAME_BASE", "https://www.mini-game.vip"
).rstrip("/")
WS_BASE = os.environ.get(
    "XOSO66_MINIGAME_WS_BASE", "wss://wss-minigame-viet.227290.com"
).rstrip("/")
GET_TOKEN_PATH = "/server/push/getToken"
PLACE_ORDER_PATH = "/server/order/placeOrder"

from xoso66_minigame_catalog import MERCHANT  # noqa: E402
from xoso66_deposit import DEFAULT_UA  # noqa: E402


def get_minigame(session: dict) -> dict[str, Any]:
    mg = session.get("minigame")
    if not isinstance(mg, dict):
        mg = {}
        session["minigame"] = mg
    return mg


def _cookie_header(mg: dict) -> str:
    cookies = mg.get("cookies") or {}
    if isinstance(cookies, str):
        return cookies.strip()
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v is not None)


def merge_minigame_cookies(mg: dict, resp: requests.Response) -> None:
    cookies = dict(mg.get("cookies") or {})
    for c in resp.cookies:
        cookies[c.name] = c.value
    mg["cookies"] = cookies


def lobby_referer(*, game_id: int, gamename: str = "lobby") -> str:
    return (
        f"{MINIGAME_BASE}/?gamename={gamename}&is_redirect=0"
        f"&merchant={MERCHANT}&x-device=pc&game_id={game_id}"
    )


def build_minigame_headers(
    session: dict,
    *,
    game_id: int = 9,
    gamename: str = "lobby",
    content_type: str | None = "application/json; charset=UTF-8",
) -> dict[str, str]:
    mg = get_minigame(session)
    user_token = (mg.get("user_token") or "").strip()
    if not user_token:
        raise ValueError("Thiếu minigame.user_token — chạy xoso66_minigame_refresh.py")

    h: dict[str, str] = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "vi,en-US;q=0.9,en;q=0.8",
        "origin": MINIGAME_BASE,
        "referer": lobby_referer(game_id=game_id, gamename=gamename),
        "user-agent": session.get("user_agent") or DEFAULT_UA,
        "user-token": user_token,
        "cookie": _cookie_header(mg),
    }
    if content_type:
        h["content-type"] = content_type
    return h


def minigame_requests_session(session: dict):
    from xoso66_proxy import apply_requests_proxy, ensure_proxy, wrap_requests_session

    ensure_proxy(session)
    s = requests.Session()
    apply_requests_proxy(s, session["proxy"])
    return wrap_requests_session(s, session, source="minigame-HTTP")


def _merge_cffi_cookies(mg: dict, resp: Any) -> None:
    cookies = dict(mg.get("cookies") or {})
    try:
        for name, value in resp.cookies.items():
            cookies[name] = value
    except Exception:
        pass
    mg["cookies"] = cookies


def minigame_request(
    session: dict,
    method: str,
    path: str,
    *,
    game_id: int = 9,
    gamename: str = "lobby",
    json_body: dict | list | None = None,
    timeout: int = 45,
) -> tuple[int, dict[str, Any]]:
    """Gọi API mini-game; trả (http_status, body_json hoặc wrapper lỗi)."""
    from xoso66_proxy import build_proxies, ensure_proxy

    url = f"{MINIGAME_BASE}{path}" if path.startswith("/") else path
    headers = build_minigame_headers(
        session,
        game_id=game_id,
        gamename=gamename,
        content_type="application/json; charset=UTF-8" if json_body is not None else None,
    )
    ensure_proxy(session)
    mg = get_minigame(session)
    t_sec = max(3, int(timeout))
    proxies = build_proxies(session["proxy"])
    m = method.upper()

    impersonate = os.environ.get("XOSO66_CF_IMPERSONATE", "chrome120")  # chrome131 hay unsupported
    try:
        from curl_cffi import requests as cffi_requests

        r = cffi_requests.request(
            m,
            url,
            impersonate=impersonate,
            headers=headers,
            json=json_body,
            cookies=mg.get("cookies") or {},
            proxies=proxies,
            timeout=t_sec,
        )
        _merge_cffi_cookies(mg, r)
        try:
            js = r.json()
        except Exception:
            return r.status_code, {
                "code": 0,
                "msg": "invalid json",
                "_raw": (r.text or "")[:500],
            }
        if not isinstance(js, dict):
            return r.status_code, {"code": 0, "msg": "invalid response", "_raw": js}
        return r.status_code, js
    except ImportError:
        pass
    except Exception:
        # curl_cffi fail → fallback requests (ProxyAwareSession: retry + báo Lỗi proxy).
        pass

    http = minigame_requests_session(session)
    r = http.request(
        m,
        url,
        headers=headers,
        json=json_body,
        timeout=(min(8, t_sec), t_sec),
    )
    merge_minigame_cookies(mg, r)
    try:
        js = r.json()
    except Exception:
        return r.status_code, {"code": 0, "msg": "invalid json", "_raw": (r.text or "")[:500]}
    if not isinstance(js, dict):
        return r.status_code, {"code": 0, "msg": "invalid response", "_raw": js}
    return r.status_code, js


def ws_url_from_token(ws_token: str) -> str:
    return f"{WS_BASE}/?token={ws_token}"


def place_minigame_order(
    session: dict,
    *,
    game_id: int,
    play_id: int,
    amount: int,
    issue: str = "",
    gamename: str = "dice_md5",
    content: str = "",
) -> dict[str, Any]:
    """Legacy wrapper — logic chính ở xoso66_minigame_bet."""
    from xoso66_minigame_catalog import game_by_id

    try:
        game_key, g = game_by_id(game_id)
    except KeyError:
        game_key, g = "", {}
    side = ""
    for s, p in (g.get("plays") or {}).items():
        if int(p.get("play_id") or 0) == int(play_id):
            side = s
            break
    if not game_key or not side:
        raise ValueError(f"game_id={game_id} play_id={play_id} chưa có trong catalog")

    from xoso66_minigame_bet import BetRequest, place_bet

    r = place_bet(
        session,
        BetRequest(game_key=game_key, side=side, amount=amount, issue=issue),
    )
    return {
        "ok": r.ok,
        "http_status": r.http_status,
        "msg": r.msg,
        "code": r.code,
        "data": r.data,
        "raw": r.raw,
    }
