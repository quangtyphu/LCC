# -*- coding: utf-8 -*-
"""Lưu cookie + URL WS h54uk từ Chrome — dùng khi tắt Chrome vẫn cược (thử nghiệm)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from c168_capture_game_b import CDP_URL
from c168_vendor_bet import _cdp_request, find_vendor_tab

_CACHE_DIR = Path(__file__).resolve().parent
CACHE_PATH = _CACHE_DIR / "c168_vendor_session_cache.json"


def cache_path_for_username(username: str = "") -> Path:
    user = str(username or "").strip().lower()
    if not user:
        return CACHE_PATH
    from c168_accounts_db import safe_dir_key

    return _CACHE_DIR / f"c168_vendor_session_{safe_dir_key(user)}.json"


def h54uk_jwt_expires_in(url: str) -> float:
    """Giây còn lại trước khi JWT trong URL h54uk hết hạn (<0 = đã hết)."""
    try:
        from urllib.parse import parse_qs

        import base64
        import json as _json

        qs = parse_qs(urlparse(url or "").query)
        token = (qs.get("token") or [""])[0]
        if not token or "." not in token:
            return -1.0
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = _json.loads(base64.urlsafe_b64decode(payload))
        exp = float(data.get("exp") or 0)
        if exp <= 0:
            return -1.0
        return exp - time.time()
    except Exception:
        return -1.0


def apply_fresh_h54uk(cap: dict[str, Any], h54uk_url: str) -> dict[str, Any]:
    """Cập nhật URL/token h54uk + origin ngay trước khi tắt Chrome."""
    out = dict(cap)
    h54 = (h54uk_url or "").strip()
    if not h54:
        return out
    out["h54uk_url"] = h54
    from c168_vendor_ws_client import origin_from_ws_url

    o2 = origin_from_ws_url(h54)
    if o2:
        out["origin"] = o2
    try:
        from c168_vendor_jk17y_client import user_id_from_h54uk_url

        uid = user_id_from_h54uk_url(h54)
        if uid:
            out["user_id"] = uid
    except Exception:
        pass
    out["saved_at"] = time.time()
    return out


def _vendor_origin(url: str) -> str:
    p = urlparse(url or "")
    if not p.scheme or not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}"


def capture_vendor_session(
    *,
    cdp_base: str = CDP_URL,
    h54uk_url: str = "",
    proxy: str = "",
    tab_hint: str = "",
) -> dict[str, Any]:
    tab = find_vendor_tab(cdp_base, require_table=False, prefer_url_contains=tab_hint)
    if not tab:
        return {"ok": False, "error": "no_vendor_tab"}

    origin = _vendor_origin(tab.get("url") or "")
    if not origin:
        return {"ok": False, "error": "no_origin", "tab": tab.get("url")}

    wss = tab["wss"]
    _cdp_request(wss, "Network.enable", {}, 1)
    resp = _cdp_request(
        wss,
        "Network.getCookies",
        {"urls": [origin, origin + "/"]},
        2,
    )
    cookies: dict[str, str] = {}
    for c in (resp.get("result") or {}).get("cookies") or []:
        if isinstance(c, dict) and c.get("name"):
            cookies[str(c["name"])] = str(c.get("value") or "")

    if not cookies.get("JSESSIONID"):
        resp2 = _cdp_request(
            wss,
            "Runtime.evaluate",
            {
                "expression": (
                    "() => Object.fromEntries("
                    "document.cookie.split(';').map(s => {"
                    " const i = s.indexOf('=');"
                    " return [s.slice(0,i).trim(), s.slice(i+1)];"
                    "}).filter(x => x[0]))"
                ),
                "returnByValue": True,
            },
            3,
        )
        val = ((resp2.get("result") or {}).get("result") or {}).get("value")
        if isinstance(val, dict):
            cookies.update({str(k): str(v) for k, v in val.items()})

    h54uk_url = (h54uk_url or "").strip()
    if not h54uk_url:
        resp3 = _cdp_request(
            wss,
            "Runtime.evaluate",
            {
                "expression": (
                    "() => {"
                    " const log = window.__c168WsLog || [];"
                    " for (let i = log.length - 1; i >= 0; i--) {"
                    "   const u = log[i].url || '';"
                    "   if (u.includes('h54uk')) return u;"
                    " }"
                    " const socks = window.__c168JkSockets || [];"
                    " for (const s of socks) {"
                    "   if ((s.url||'').includes('h54uk')) return s.url;"
                    " }"
                    " return '';"
                    "}"
                ),
                "returnByValue": True,
            },
            4,
        )
        val3 = ((resp3.get("result") or {}).get("result") or {}).get("value")
        if isinstance(val3, str):
            h54uk_url = val3

    user_id = ""
    if h54uk_url:
        try:
            from c168_vendor_jk17y_client import user_id_from_h54uk_url

            user_id = user_id_from_h54uk_url(h54uk_url)
        except Exception:
            pass
    jk17y_url = ""
    try:
        from c168_vendor_ws_sniff import take_active_sniffer

        sn = take_active_sniffer()
        if sn and getattr(sn, "jk17y_urls", None):
            jk17y_url = str(sn.jk17y_urls[-1])
    except Exception:
        pass
    if not jk17y_url:
        jk17y_url = "wss://tel617.delta9968.com/jk17y/"

    if h54uk_url:
        from c168_vendor_ws_client import origin_from_ws_url

        o2 = origin_from_ws_url(h54uk_url)
        if o2:
            origin = o2

    data = {
        "ok": bool(cookies.get("JSESSIONID")) and bool(h54uk_url),
        "origin": origin,
        "cookies": cookies,
        "h54uk_url": h54uk_url,
        "jk17y_url": jk17y_url,
        "user_id": user_id,
        "referer": tab.get("url") or f"{origin}/player/singleBacTable.jsp?dm=1",
        "proxy": (proxy or "").strip(),
        "saved_at": time.time(),
    }
    if data["ok"]:
        save_vendor_session(data, username=str(data.get("username") or ""))
    return data


def save_vendor_session(data: dict[str, Any], *, username: str = "") -> None:
    path = cache_path_for_username(username)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if username:
        CACHE_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def load_vendor_session(*, username: str = "") -> dict[str, Any] | None:
    user = str(username or "").strip()
    paths: list[Path] = []
    if user:
        paths.append(cache_path_for_username(user))
    paths.append(CACHE_PATH)
    for path in paths:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return None


def refresh_session_from_chrome(
    *,
    username: str,
    h54uk_url: str = "",
    proxy: str = "",
) -> dict[str, Any]:
    """Lấy cookie + h54uk mới từ Chrome acc đang chạy (sau login / vào game)."""
    user = str(username or "").strip()
    if not user:
        return {"ok": False, "error": "no_username"}
    try:
        from c168_capture_game_b import _cdp_alive
        from c168_chrome_session import resolve_chrome_session
    except ImportError as e:
        return {"ok": False, "error": str(e)}

    cms = resolve_chrome_session(username=user)
    if not cms:
        return {"ok": False, "error": f"no_account:{user}"}
    cdp = cms.cdp_url.rstrip("/")
    if not _cdp_alive(cdp):
        return {"ok": False, "error": "chrome_not_running", "cdp_url": cdp}

    h54 = (h54uk_url or "").strip()
    if not h54:
        try:
            from c168_vendor_ws_sniff import take_active_sniffer

            sn = take_active_sniffer()
            if sn and sn.h54uk_urls:
                h54 = str(sn.h54uk_urls[-1])
        except Exception:
            pass

    cap = capture_vendor_session(
        cdp_base=cdp,
        h54uk_url=h54,
        proxy=(proxy or cms.proxy or "").strip(),
    )
    if cap.get("ok"):
        cap["username"] = user
        save_vendor_session(cap, username=user)
    return cap
