#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vào sảnh Game B và in toàn bộ frame WS h54uk sent/recv."""

from __future__ import annotations

import argparse
import base64
import os
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent
_REPO = _ROOT.parent
os.chdir(_ROOT)
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from allgame.db.accounts_db import get_account
from allgame.portals.c168.open_chrome_token import ensure_chrome_for_username

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
      body: JSON.stringify({ os_type: 3, callContext: "trace_h54uk_ws", time: now }),
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


def _short(v: Any, n: int = 500) -> str:
    s = str(v or "").replace("\n", " ").strip()
    return s[:n] + ("..." if len(s) > n else "")


def _decode_ws_payload(raw: str) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    # Nhiều frame binary đi dạng base64; cố decode để đọc router/event/tableID.
    try:
        # Bổ sung padding nếu thiếu
        pad = len(s) % 4
        if pad:
            s2 = s + ("=" * (4 - pad))
        else:
            s2 = s
        b = base64.b64decode(s2, validate=False)
        try:
            txt = b.decode("utf-8", errors="ignore")
        except Exception:
            txt = str(b)
        if "{" in txt:
            return txt[txt.find("{") :].strip()
        if txt.strip():
            return txt.strip()
    except Exception:
        pass
    return s


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    os.environ.setdefault("PYTHONUNBUFFERED", "1")

    ap = argparse.ArgumentParser(description="Trace raw WS h54uk sent/recv")
    ap.add_argument("--username", required=True)
    ap.add_argument("--duration", type=int, default=0, help="0 = nghe liên tục")
    args = ap.parse_args()

    user = str(args.username or "").strip()
    acc = get_account("c168", user)
    if not acc:
        print({"ok": False, "error": "account_not_found", "username": user})
        return 1

    chrome = ensure_chrome_for_username(user, account=acc)
    if not chrome.get("ok"):
        print({"ok": False, "error": "ensure_chrome_failed", "detail": chrome})
        return 1

    cdp_url = str(chrome.get("cdp_url") or "")
    if not cdp_url:
        print({"ok": False, "error": "missing_cdp_url"})
        return 1

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print({"ok": False, "error": "playwright_not_installed"})
        return 1

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        contexts = list(browser.contexts) if browser.contexts else [browser.new_context()]
        context = contexts[0]
        page = context.pages[0] if context.pages else context.new_page()
        if "c168b2.cc" not in str(page.url or "").lower():
            page.goto("https://c168b2.cc/", wait_until="domcontentloaded", timeout=120_000)
        out = page.evaluate(_JS_OPEN_GAME_B_LOBBY, {"platformId": 1012, "gameId": 10120000})
        if not isinstance(out, dict) or not out.get("ok"):
            print({"ok": False, "error": "open_game_b_failed", "detail": out})
            return 1
        game_url = str(out.get("game_url") or "")
        if not game_url:
            print({"ok": False, "error": "empty_game_url"})
            return 1
        page.goto(game_url, wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(1500)
        try:
            page.bring_to_front()
        except Exception:
            pass

        ws_map: dict[str, str] = {}
        cdp_sessions = []
        recv_count = {"n": 0}
        sent_count = {"n": 0}

        def _bind(pg) -> None:
            try:
                cdp = context.new_cdp_session(pg)
                cdp.send("Network.enable")
                cdp_sessions.append(cdp)
            except Exception:
                return

            def _on_ws_created(params: dict[str, Any]) -> None:
                rid = str(params.get("requestId") or "")
                ws_url = str(params.get("url") or "")
                if rid:
                    ws_map[rid] = ws_url
                if "h54uk" in ws_url.lower():
                    print(f"[WS OPEN] {ws_url}", flush=True)

            def _on_recv(params: dict[str, Any]) -> None:
                rid = str(params.get("requestId") or "")
                ws_url = str(ws_map.get(rid) or "")
                if ws_url and "h54uk" not in ws_url.lower():
                    return
                raw = str((params.get("response") or {}).get("payloadData") or "")
                decoded = _decode_ws_payload(raw)
                recv_count["n"] += 1
                tag = ws_url if ws_url else f"UNKNOWN:{rid}"
                print(f"[WS RECV][{tag}] {_short(decoded)}", flush=True)

            def _on_sent(params: dict[str, Any]) -> None:
                rid = str(params.get("requestId") or "")
                ws_url = str(ws_map.get(rid) or "")
                if ws_url and "h54uk" not in ws_url.lower():
                    return
                raw = str((params.get("response") or {}).get("payloadData") or "")
                decoded = _decode_ws_payload(raw)
                sent_count["n"] += 1
                tag = ws_url if ws_url else f"UNKNOWN:{rid}"
                print(f"[WS SENT][{tag}] {_short(decoded)}", flush=True)

            cdp.on("Network.webSocketCreated", _on_ws_created)
            cdp.on("Network.webSocketFrameReceived", _on_recv)
            cdp.on("Network.webSocketFrameSent", _on_sent)

        # Bind toàn bộ contexts/pages để không hụt WS khi tab chạy ở context khác.
        for ctx in contexts:
            for pg in list(ctx.pages):
                _bind(pg)
                try:
                    pg.on(
                        "websocket",
                        lambda ws: print(f"[WS OPEN][PW] {ws.url}", flush=True),
                    )
                except Exception:
                    pass
            ctx.on("page", _bind)
            try:
                ctx.on(
                    "page",
                    lambda pg: pg.on(
                        "websocket",
                        lambda ws: print(f"[WS OPEN][PW] {ws.url}", flush=True),
                    ),
                )
            except Exception:
                pass

        # Quan trọng: reload sau khi đã bind để WS được tạo mới dưới hook hiện tại.
        try:
            page.reload(wait_until="domcontentloaded", timeout=120_000)
            page.wait_for_timeout(1200)
        except Exception:
            pass

        print("[TRACE] Đã vào sảnh Game B (reload sau khi bind). Đang in toàn bộ WS h54uk sent/recv...", flush=True)
        started = time.time()
        last_ping = 0.0
        try:
            while True:
                if int(args.duration or 0) > 0 and (time.time() - started) >= int(args.duration):
                    break
                now = time.time()
                if now - last_ping >= 10:
                    last_ping = now
                    print(
                        f"[TRACE] heartbeat recv={recv_count['n']} sent={sent_count['n']} mapped_ws={len(ws_map)}",
                        flush=True,
                    )
                time.sleep(0.4)
        except KeyboardInterrupt:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

