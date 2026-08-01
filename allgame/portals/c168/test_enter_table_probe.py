#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Probe vào bàn C168: dừng ở sảnh, click tay, script đọc tín hiệu."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent
_REPO = _ROOT.parent.parent.parent
if str(_ROOT) in sys.path:
    sys.path.remove(str(_ROOT))
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from allgame.db.accounts_db import get_account
from allgame.portals.c168.open_chrome_token import ensure_chrome_for_username

_API_HINT_RE = re.compile(
    r"/hall/api/gameCenter/gameApi/(login|logout)|/player/update/setUserSingleTableID|/player/query/queryInitTableInfo|/player/singleBacTable\\.jsp|/player/query/",
    re.I,
)
_WS_HINT_RE = re.compile(r"h54uk|jk17y|GP_NEW_GAME_START|GP_WINNER|round|shoe|table", re.I)
_VENDOR_URL_RE = re.compile(r"/player/webMain|/player/singleBacTable|intplaynet\\.com/player|bpcdf\\.|tgmeq|mhuxu|vesnamex", re.I)

_JS_OPEN_GAME_B_LOBBY = """
async ({ platformId, gameId }) => {
  const hall = "https://af861c.c168f.com";
  const origin = location.origin || "https://c168b2.cc";
  const readToken = () => {
    try {
      const raw = localStorage.getItem("web__lobby__persisted__token");
      if (!raw) return null;
      return JSON.parse(decodeURIComponent(raw));
    } catch (e) {
      return null;
    }
  };
  const tokenBox = readToken();
  const infos = tokenBox && tokenBox.tokenInfos;
  if (!infos || !infos.session_key) return { ok: false, error: "missing_session_key" };
  const jwt = String(infos.jwt_token || "");
  if (jwt.length < 40) return { ok: false, error: "missing_newjwt" };
  let device = "";
  try {
    const d = localStorage.getItem("web__lobby__persisted__device");
    device = d ? JSON.parse(decodeURIComponent(d)).uuid || "" : "";
  } catch (e) {}
  const headers = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json",
    "origin": origin,
    "referer": origin + "/",
    "token": infos.session_key,
    "newjwt": jwt,
    "sitecode": "2865",
    "domain": "c168b2.cc",
    "currency": "VND",
    "device": device,
    "x-data-mode": "plain",
    "x-version": "7.3.17",
    "appversion": "v7.3.17",
    "x-device": "1-1",
    "timestamp": String(Math.floor(Date.now() / 1000)),
  };
  const now = Math.floor(Date.now() / 1000);
  try {
    await fetch(hall + "/hall/api/gameCenter/gameApi/logout", {
      method: "POST",
      headers,
      body: JSON.stringify({ os_type: 3, callContext: "probe_open_gameb", time: now }),
    });
  } catch (e) {}
  await new Promise((r) => setTimeout(r, 350));
  const bodyLogin = {
    os_type: 3,
    gameid: gameId,
    platfromid: platformId,
    exitUrl: "",
    cid: "999998",
    time: now,
  };
  const resp = await fetch(hall + "/hall/api/gameCenter/gameApi/login", {
    method: "POST",
    headers,
    body: JSON.stringify(bodyLogin),
  });
  const txt = await resp.text();
  let data = null;
  try { data = JSON.parse(txt); } catch (e) {}
  if (!(resp.ok && data && data.code === 1)) {
    return { ok: false, error: "gameApi_login_fail", status: resp.status, body: txt.slice(0, 260) };
  }
  const gameUrl =
    (data.data && (data.data.game_url || (data.data.url && data.data.url[0] && data.data.url[0].url))) ||
    "";
  if (!gameUrl) return { ok: false, error: "no_game_url", body: txt.slice(0, 260) };
  return { ok: true, game_url: gameUrl };
}
"""


def _short(x: Any, n: int = 260) -> str:
    s = str(x or "").replace("\n", " ").strip()
    return s[:n] + ("..." if len(s) > n else "")


def _cdp_alive(cdp_url: str) -> bool:
    try:
        import urllib.request

        with urllib.request.urlopen(f"{cdp_url.rstrip('/')}/json/version", timeout=2) as resp:
            return int(resp.status or 0) == 200
    except Exception:
        return False


def _set_skip_debugger_pause(context, page) -> None:
    try:
        cdp = context.new_cdp_session(page)
        cdp.send("Debugger.enable")
        cdp.send("Debugger.setSkipAllPauses", {"skip": True})
        try:
            cdp.send("Runtime.runIfWaitingForDebugger")
        except Exception:
            pass
        try:
            cdp.send("Debugger.resume")
        except Exception:
            pass
    except Exception:
        pass


def run_probe(username: str, duration_sec: int = 180) -> dict[str, Any]:
    user = str(username or "").strip()
    acc = get_account("c168", user)
    if not acc:
        return {"ok": False, "error": "account_not_found", "username": user}

    chrome = ensure_chrome_for_username(user, account=acc)
    if not chrome.get("ok"):
        return {"ok": False, "error": "ensure_chrome_failed", "detail": chrome}

    cdp_url = str(chrome.get("cdp_url") or "")
    if not cdp_url:
        return {"ok": False, "error": "missing_cdp_url"}

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return {
            "ok": False,
            "error": "playwright_not_installed",
            "hint": "pip install playwright && playwright install chromium",
        }

    api_hits: list[dict[str, Any]] = []
    ws_hits: list[dict[str, Any]] = []
    print(
        f"[PROBE] User={user} | cdp={cdp_url}\n"
        "[PROBE] Đang mở sảnh C168. Hãy click vào bàn (vd C06).\n"
        "[PROBE] Script sẽ gom log, và chỉ in đầy đủ khi bạn tắt Chrome.",
        flush=True,
    )

    stop_reason = "timeout"
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            return {"ok": False, "error": f"connect_cdp_failed:{e}", "cdp_url": cdp_url}

        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
        _set_skip_debugger_pause(context, page)
        try:
            if "c168b2.cc" not in str(page.url or "").lower():
                page.goto("https://c168b2.cc/", wait_until="domcontentloaded", timeout=120_000)
            try:
                page.bring_to_front()
            except Exception:
                pass
            # Đưa vào sảnh Game B trước khi user click bàn.
            gameb = page.evaluate(_JS_OPEN_GAME_B_LOBBY, {"platformId": 1012, "gameId": 10120000})
            if not isinstance(gameb, dict) or not gameb.get("ok"):
                return {"ok": False, "error": "open_game_b_failed", "detail": gameb}
            game_url = str(gameb.get("game_url") or "")
            if not game_url:
                return {"ok": False, "error": "empty_game_url"}
            page.goto(game_url, wait_until="domcontentloaded", timeout=120_000)
            page.wait_for_timeout(2500)
            # Nếu redirect tạo tab mới thì chọn tab vendor.
            for pg in list(context.pages):
                if _VENDOR_URL_RE.search(str(pg.url or "")):
                    page = pg
                    break
            _set_skip_debugger_pause(context, page)
            try:
                page.bring_to_front()
            except Exception:
                pass
        except Exception:
            # vẫn giữ probe để bắt traffic nếu tab đổi/rụng tạm
            pass

        def _on_request(req):
            u = str(req.url or "")
            if _API_HINT_RE.search(u):
                body = ""
                try:
                    body = req.post_data or ""
                except Exception:
                    body = ""
                hit = {"kind": "request", "method": req.method, "url": u, "body": _short(body)}
                api_hits.append(hit)

        def _on_response(resp):
            u = str(resp.url or "")
            if _API_HINT_RE.search(u):
                txt = ""
                try:
                    txt = resp.text()
                except Exception:
                    txt = ""
                hit = {
                    "kind": "response",
                    "status": int(resp.status or 0),
                    "url": u,
                    "body": _short(txt),
                }
                api_hits.append(hit)

        def _bind_ws(ws):
            ws_url = str(ws.url or "")
            ws_hits.append({"kind": "open", "ws_url": ws_url})

            def _on_frame(payload):
                text = ""
                try:
                    text = getattr(payload, "payload", payload)
                except Exception:
                    text = str(payload)
                if _WS_HINT_RE.search(str(text)):
                    hit = {"kind": "frame", "ws_url": ws_url, "frame": _short(text)}
                    ws_hits.append(hit)

            try:
                ws.on("framereceived", _on_frame)
            except Exception:
                pass

        def _bind_page_events(pg) -> None:
            try:
                pg.on("request", _on_request)
                pg.on("response", _on_response)
                pg.on("websocket", _bind_ws)
            except Exception:
                pass

        for pg in list(context.pages):
            _bind_page_events(pg)
        context.on("page", lambda pg: (_set_skip_debugger_pause(context, pg), _bind_page_events(pg)))

        deadline = time.time() + max(30, int(duration_sec))
        lost_since = 0.0
        while time.time() < deadline:
            if _cdp_alive(cdp_url):
                lost_since = 0.0
            else:
                if lost_since <= 0:
                    lost_since = time.time()
                elif time.time() - lost_since > 6.0:
                    stop_reason = "cdp_lost"
                    break
            time.sleep(0.5)

    print(f"[PROBE] Kết thúc capture ({stop_reason}).", flush=True)
    for hit in api_hits:
        if hit.get("kind") == "request":
            print(
                f"[PROBE][API][REQ] {hit.get('method')} {hit.get('url')} | body={_short(hit.get('body'), 180)}",
                flush=True,
            )
        else:
            print(
                f"[PROBE][API][RES] {hit.get('status')} {hit.get('url')} | body={_short(hit.get('body'), 180)}",
                flush=True,
            )
    for hit in ws_hits:
        if hit.get("kind") == "open":
            print(f"[PROBE][WS][OPEN] {hit.get('ws_url')}", flush=True)
        else:
            print(f"[PROBE][WS][FRAME] {_short(hit.get('frame'), 180)}", flush=True)

    final_urls = []
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            if _cdp_alive(cdp_url):
                b2 = p.chromium.connect_over_cdp(cdp_url)
                ctx2 = b2.contexts[0] if b2.contexts else b2.new_context()
                final_urls = [str(pg.url or "") for pg in ctx2.pages][-10:]
    except Exception:
        final_urls = []

    return {
        "ok": True,
        "username": user,
        "duration_sec": int(duration_sec),
        "api_hits_count": len(api_hits),
        "ws_hits_count": len(ws_hits),
        "api_hits_preview": api_hits[-20:],
        "ws_hits_preview": ws_hits[-20:],
        "final_urls": final_urls,
        "stop_reason": stop_reason,
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    os.environ.setdefault("PYTHONUNBUFFERED", "1")

    ap = argparse.ArgumentParser(description="Probe click vào bàn C168 từ sảnh")
    ap.add_argument("--username", required=True, help="Username c168 trong allgame DB")
    ap.add_argument("--duration", type=int, default=180, help="Thời gian nghe (giây)")
    args = ap.parse_args()

    out = run_probe(args.username, duration_sec=args.duration)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

