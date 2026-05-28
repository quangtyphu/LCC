# -*- coding: utf-8 -*-
"""
Mở game 3D (play.3dbenbet.net) và chuẩn bị kết nối SignalR Tài Xỉu.

Luồng site (sau login):
  1. POST api.bencloud.io + encrypt (đã có benbet_login)
  2. POST game.bencloud.io/game/get_url  body: { id: "<game_menu_id>" }
  3. Mở URL trả về → Cocos load + negotiate + WebSocket LuckyDiceHub

Game Tài Xỉu Cân Bảng (menu):
  - game_data id: 14001, gamecode: 8
  - Hub SignalR: LuckyDiceHub, subdomain: tai → tai.3dbenbet.net
"""
from __future__ import annotations

import json
import uuid
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from benbet_crypto import decrypt_body, encrypt_body
from benbet_login import API_BASE, default_headers, login
from benbet_proxy import BenbetProxy

GAME_API = "https://game.bencloud.io"
PLAY_ORIGIN = "https://play.3dbenbet.net"
HOST_3D = "3dbenbet.net"

# Tài Xỉu Cân Bảng — lấy từ /news/game_data
GAME_TAI_XIU_CAN_BANG = "14001"
# Hub/subdomain khớp bundle HubName.ts + SubdomainName.ts (không phải tên hiển thị)
HUB_TAI_XIU = "luckydiceHub"
SUBDOMAIN_TAI_XIU = "taixiu"  # bundle: SubdomainName.TAI_XIU = "taixiu."
# MethodHubName.ts — tên method trên wire (PascalCase, không phải BET/ENTER_LOBBY)
HUB_METHOD_ENTER_LOBBY = "EnterLobby"
HUB_METHOD_BET = "Bet"
HUB_METHOD_CORD_INFO = "CordInfo"


def _api_post(
    base: str,
    path: str,
    inner: dict[str, Any],
    token: str,
    *,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    enc = encrypt_body(inner)
    sess = session or requests.Session()
    r = sess.post(
        f"{base}{path}",
        headers=default_headers(token=token),
        json={"data": enc},
        timeout=30,
    )
    r.raise_for_status()
    text = (r.text or "").strip()
    if text and "html" in (r.headers.get("content-type") or "").lower():
        return decrypt_body(text)
    return r.json()


def fetch_game_catalog(token: str, *, session: requests.Session | None = None) -> list[dict]:
    """Danh sách game từ POST /news/game_data."""
    inner = {"platform": None, "channel": None, "aaid": str(uuid.uuid4())}
    raw = _api_post(API_BASE, "/news/game_data", inner, token, session=session)
    if raw.get("code") not in (0, "0"):
        raise RuntimeError(f"game_data: {raw.get('message')} ({raw.get('code')})")
    out: list[dict] = []
    for cat in (raw.get("data") or {}).get("list") or []:
        for g in cat.get("game_list") or []:
            out.append(
                {
                    "id": g.get("id"),
                    "name": g.get("name"),
                    "gamecode": g.get("gamecode"),
                    "platform": g.get("platform"),
                    "category": cat.get("name"),
                }
            )
    return out


def get_game_launch_url(
    game_menu_id: str,
    token: str,
    *,
    session: requests.Session | None = None,
    aaid: str | None = None,
) -> str:
    """
    POST game.bencloud.io/game/get_url — tham số bắt buộc: id (không phải gamecode).

    Trả URL dạng:
      https://play.3dbenbet.net?gameid=8&username=...&access_token=...&session_token=...
    """
    inner = {
        "platform": None,
        "channel": None,
        "aaid": aaid or str(uuid.uuid4()),
        "id": str(game_menu_id),
    }
    raw = _api_post(GAME_API, "/game/get_url", inner, token, session=session)
    if raw.get("code") not in (0, "0"):
        raise RuntimeError(f"get_url: {raw.get('message')} ({raw.get('code')})")
    url = (raw.get("data") or {}).get("url")
    if not url:
        raise RuntimeError("get_url không có data.url")
    return str(url)


def parse_launch_url(url: str) -> dict[str, str]:
    q = parse_qs(urlparse(url).query)
    return {k: (v[0] if v else "") for k, v in q.items()}


def signalr_negotiate(
    subdomain: str,
    *,
    access_token: str,
    session_token: str,
    username: str = "",
    lang: str = "vn",
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """
    GET https://{subdomain}.3dbenbet.net/signalr/negotiate?access_token=...&session_token=...
    Trả JSON có ConnectionToken (dùng cho WebSocket).
    """
    params: dict[str, str] = {
        "access_token": access_token,
        "session_token": session_token,
        "language": lang,
    }
    if username:
        params["username"] = username
    url = f"https://{subdomain}.{HOST_3D}/signalr/negotiate"
    sess = session or requests.Session()
    r = sess.get(
        url,
        params=params,
        headers={
            "User-Agent": default_headers()["user-agent"],
            "Accept": "application/json",
            "Origin": PLAY_ORIGIN,
            "Referer": f"{PLAY_ORIGIN}/",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def build_signalr_ws_url(
    *,
    subdomain: str = SUBDOMAIN_TAI_XIU,
    hub_name: str = HUB_TAI_XIU,
    connection_token: str,
    access_token: str,
    session_token: str,
    username: str = "",
    lang: str = "vn",
    reconnect: bool = False,
    tid: str = "1",
) -> str:
    """URL WebSocket SignalR (transport=webSockets) — khớp browser / bundle Cocos."""
    import json as _json
    from urllib.parse import quote

    conn_data = quote(_json.dumps([{"name": hub_name}], separators=(",", ":")))
    path = "signalr/reconnect" if reconnect else "signalr/connect"
    url = (
        f"wss://{subdomain}.{HOST_3D}/{path}"
        f"?transport=webSockets"
        f"&connectionToken={quote(connection_token, safe='')}"
        f"&connectionData={conn_data}"
        f"&tid={tid}"
        f"&access_token={quote(access_token, safe='')}"
        f"&session_token={quote(session_token, safe='')}"
    )
    if username:
        url += f"&username={quote(username, safe='')}"
    if lang:
        url += f"&language={quote(lang)}"
    return url


def open_tai_xiu_session(
    username: str,
    password: str,
    *,
    game_menu_id: str = GAME_TAI_XIU_CAN_BANG,
    proxy: str | None = None,
) -> dict[str, Any]:
    """
    Login → launch URL → negotiate (nếu DNS/subdomain OK).

    Trả dict: ok, lt, launch_url, launch_params, negotiate, ws_url (nếu negotiate OK)
    """
    lg = login(username, password, proxy=proxy)
    if not lg.get("ok"):
        return {"ok": False, "login": lg}

    bp = BenbetProxy.from_string(proxy)
    http_sess = requests.Session()
    if bp:
        bp.mount_session(http_sess)

    token = str(lg.get("lt") or "")
    launch = get_game_launch_url(game_menu_id, token, session=http_sess)
    params = parse_launch_url(launch)
    access = params.get("access_token", "")
    session_tok = params.get("session_token", token)
    user = params.get("username", username)

    out: dict[str, Any] = {
        "ok": True,
        "lt": token,
        "user_info": lg.get("user_info"),
        "launch_url": launch,
        "launch_params": params,
        "hub": HUB_TAI_XIU,
        "subdomain": SUBDOMAIN_TAI_XIU,
    }

    try:
        neg = signalr_negotiate(
            SUBDOMAIN_TAI_XIU,
            access_token=access,
            session_token=session_tok,
            username=user,
            session=http_sess,
        )
        out["negotiate"] = neg
        conn = neg.get("ConnectionToken") or neg.get("connectionToken")
        if conn:
            out["ws_url"] = build_signalr_ws_url(
                connection_token=str(conn),
                access_token=access,
                session_token=session_tok,
                username=user,
            )
    except Exception as exc:
        out["negotiate_error"] = str(exc)

    if bp:
        from benbet_proxy import proxy_label

        out["proxy"] = proxy_label(bp.raw)
    return out


# Sự kiện WS Tài Xỉu (đối chiếu LC79 / xoso66) — từ onHubMessage LuckyDiceHub
TAIXIU_WS_EVENTS = {
    "SESSION_INFO": "Phiên / phase hiện tại + tổng cược Tài-Xỉu (SessionID, Phrase, ...)",
    "NOTIFY_CHANGE_PHRASE": "Đổi phase — tương đương phiên mới / hết cược",
    "SESSION_RESULT": "Kết quả phiên",
    "currSessionInfo": "Thông tin phiên đang chạy",
    "updateTimer": "Đếm ngược",
    "betSuccess": "Đặt cược OK",
    "winResult": "Thắng phiên — Award, Balance mới",
    "playerBetList": "Danh sách cược phòng",
}
