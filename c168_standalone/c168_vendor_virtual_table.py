# -*- coding: utf-8 -*-
"""
Giả "đang ở sảnh / vào bàn" qua WS lobby (jk17y) — không cần click UI.

Capture: click C06 = gửi router ``lobbyTableClick`` (base64 JSON, tableID=1006).
Kèm ``lobbyLobbyView`` (visibleheight/width) để server coi tab vẫn active.
"""
from __future__ import annotations

import base64
import json
from typing import Any

from c168_capture_game_b import CDP_URL
from c168_vendor_bet import _cdp_request, find_vendor_tab

DEFAULT_WEBSITE = "327"
DEFAULT_CURRENCY = "11"
DEFAULT_HALL = 0
DEFAULT_GAME_GROUP = 2


def _b64_json(obj: dict[str, Any]) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def build_table_click_payload(
    table_id: int,
    *,
    user_id: str,
    website: str = DEFAULT_WEBSITE,
    currency: str = DEFAULT_CURRENCY,
    account_create_date: str = "1779615398",
    hall: int = DEFAULT_HALL,
    game_group: int = DEFAULT_GAME_GROUP,
    online: int = 2042,
) -> dict[str, Any]:
    return {
        "category": "Lobby",
        "label": "TableClick",
        "userID": user_id,
        "website": website,
        "currency": currency,
        "accountCreateDate": account_create_date,
        "hall": hall,
        "gameGroup": game_group,
        "game": 0,
        "tableID": int(table_id),
        "stage": 0,
        "dealerEvent": 7,
        "online": online,
        "device": 4,
    }


def build_lobby_view_payload(
    *,
    user_id: str,
    website: str = DEFAULT_WEBSITE,
    currency: str = DEFAULT_CURRENCY,
    account_create_date: str = "1779615398",
    hall: int = DEFAULT_HALL,
    game_group: int = DEFAULT_GAME_GROUP,
    width: int = 1440,
    height: int = 900,
) -> dict[str, Any]:
    return {
        "category": "Lobby",
        "label": "LobbyView",
        "userID": user_id,
        "website": website,
        "currency": currency,
        "accountCreateDate": account_create_date,
        "height": height,
        "width": width,
        "visibleheight": height,
        "visiblewidth": width,
        "autoSubmitMode": "0",
        "gameGroup": game_group,
        "hall": hall,
        "uach": "{}",
    }


def decode_user_id_from_h54uk_token(token: str) -> str:
    """JWT h54uk → userid (ecw2865agent…)."""
    parts = (token or "").split(".")
    if len(parts) < 2:
        return ""
    payload = parts[1]
    pad = "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + pad))
    except Exception:
        return ""
    return str(data.get("userid") or data.get("sub") or data.get("userID") or "")


JS_READ_H54_USER = r"""
() => {
  const fromUrl = (u) => {
    try {
      const t = new URL(u).searchParams.get('token');
      if (!t) return '';
      const p = t.split('.')[1];
      if (!p) return '';
      const pad = '='.repeat((4 - p.length % 4) % 4);
      const j = JSON.parse(atob(p.replace(/-/g,'+').replace(/_/g,'/') + pad));
      return j.userid || j.sub || j.userID || '';
    } catch (e) { return ''; }
  };
  const log = window.__c168WsLog || [];
  for (let i = log.length - 1; i >= 0; i--) {
    const u = log[i].url || '';
    if (u.includes('h54uk')) {
      const id = fromUrl(u);
      if (id) return { userID: id, source: 'ws_log' };
    }
  }
  if (window.__c168JkSockets) {
    for (const s of window.__c168JkSockets) {
      if ((s.url||'').includes('h54uk')) {
        const id = fromUrl(s.url);
        if (id) return { userID: id, source: 'jk_socket' };
      }
    }
  }
  return { userID: '', source: 'none' };
}
"""

LOBBY_WS_SEND_JS = r"""
(meta) => {
  const sendRouter = (router, key, inner) => {
    const socks = (window.__c168JkSockets || []).filter(
      s => s.ws && s.ws.readyState === 1 && /jk17y|delta9968\.com\/jk/i.test(s.url || '')
    );
    if (!socks.length) return { ok: false, error: 'no_open_jk17y', router };
    const b64 = btoa(unescape(encodeURIComponent(JSON.stringify(inner))));
    const msg = JSON.stringify({ router, data: { [key]: b64 } });
    socks[0].ws.send(msg);
    return { ok: true, router, url: socks[0].url };
  };
  const uid = meta.userID || '';
  if (!uid) return { ok: false, error: 'no_user_id' };
  const view = {
    category: 'Lobby', label: 'LobbyView', userID: uid,
    website: meta.website || '327', currency: meta.currency || '11',
    accountCreateDate: meta.accountCreateDate || '1779615398',
    height: meta.height || 900, width: meta.width || 1440,
    visibleheight: meta.visibleheight || 900, visiblewidth: meta.visiblewidth || 1440,
    autoSubmitMode: '0', gameGroup: meta.gameGroup ?? 2, hall: meta.hall ?? 0,
    uach: '{}'
  };
  const click = {
    category: 'Lobby', label: 'TableClick', userID: uid,
    website: meta.website || '327', currency: meta.currency || '11',
    accountCreateDate: meta.accountCreateDate || '1779615398',
    hall: meta.hall ?? 0, gameGroup: meta.gameGroup ?? 2, game: 0,
    tableID: Number(meta.tableID || 1006), stage: 0, dealerEvent: 7,
    online: meta.online ?? 2042, device: 4
  };
  const r1 = sendRouter('lobbyLobbyView', 'lobbyLobbyView', view);
  const r2 = sendRouter('lobbyTableClick', 'lobbyTableClick', click);
  return { ok: r1.ok && r2.ok, lobbyView: r1, tableClick: r2 };
}
"""


def _read_user_id_via_cdp(wss: str) -> str:
    _cdp_request(wss, "Runtime.enable", {}, 1)
    resp = _cdp_request(
        wss,
        "Runtime.evaluate",
        {"expression": JS_READ_H54_USER, "returnByValue": True},
        2,
    )
    val = ((resp.get("result") or {}).get("result") or {}).get("value")
    if isinstance(val, dict):
        return str(val.get("userID") or "")
    return ""


def fake_enter_table_via_cdp(
    table_id: int = 1006,
    *,
    user_id: str = "",
    cdp_base: str = CDP_URL,
    send_lobby_view: bool = True,
) -> dict[str, Any]:
    """
    Gửi lobbyLobbyView + lobbyTableClick trên WS jk17y (giả vào bàn).
    Cần Chrome đã mở game vendor (socket jk17y đang OPEN).
    """
    tab = find_vendor_tab(cdp_base, require_table=False)
    if not tab:
        return {"ok": False, "error": "no_vendor_tab"}

    wss = tab["wss"]
    uid = (user_id or "").strip() or _read_user_id_via_cdp(wss)
    if not uid:
        try:
            from c168_vendor_session_cache import load_vendor_session
            from c168_vendor_jk17y_client import user_id_from_h54uk_url

            cached = load_vendor_session() or {}
            uid = str(cached.get("user_id") or "").strip()
            if not uid:
                uid = user_id_from_h54uk_url(str(cached.get("h54uk_url") or ""))
        except Exception:
            pass
    if not uid:
        return {
            "ok": False,
            "error": "no_user_id",
            "hint": "Chưa có JWT h54uk — mở game B và đợi WS sảnh trước",
            "tab": tab.get("url"),
        }

    meta = {
        "userID": uid,
        "tableID": int(table_id),
        "website": DEFAULT_WEBSITE,
        "currency": DEFAULT_CURRENCY,
        "sendLobbyView": send_lobby_view,
    }
    _cdp_request(wss, "Runtime.enable", {}, 1)
    resp = _cdp_request(
        wss,
        "Runtime.evaluate",
        {"expression": f"({LOBBY_WS_SEND_JS})({json.dumps(meta)})", "returnByValue": True},
        3,
    )
    val = ((resp.get("result") or {}).get("result") or {}).get("value")
    if not isinstance(val, dict):
        return {"ok": False, "error": "cdp_eval_failed", "raw": resp, "userID": uid}
    val["userID"] = uid
    val["tab"] = tab.get("url")
    return val


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Giả vào bàn qua WS lobbyTableClick")
    ap.add_argument("--table-id", type=int, default=1006)
    ap.add_argument("--user-id", default="")
    args = ap.parse_args()
    out = fake_enter_table_via_cdp(args.table_id, user_id=args.user_id)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())


def maintain_virtual_table(
    table_id: int = 1006,
    *,
    user_id: str = "",
    cdp_base: str = CDP_URL,
) -> dict[str, Any]:
    """Gửi lại lobby view + table click (giữ trạng thái đang ở bàn)."""
    return fake_enter_table_via_cdp(
        table_id, user_id=user_id, cdp_base=cdp_base, send_lobby_view=True
    )
