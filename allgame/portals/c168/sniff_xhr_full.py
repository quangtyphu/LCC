#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sniff full XHR/Fetch bằng CDP Network (giống DevTools hơn)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
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
      body: JSON.stringify({ os_type: 3, callContext: "sniff_open_gameb", time: now }),
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


def _now_tag() -> str:
    return datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


def _short(x: Any, n: int = 300) -> str:
    s = str(x or "").replace("\n", " ").strip()
    return s[:n] + ("..." if len(s) > n else "")


def run_sniff(username: str, duration_sec: int = 180) -> dict[str, Any]:
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

    logs_dir = _ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    out_path = logs_dir / f"xhr_full_{user}_{_now_tag()}.jsonl"

    total_hits = 0
    req_index: dict[str, dict[str, Any]] = {}
    cdp_sessions = []

    def _write(evt: dict[str, Any]) -> None:
        nonlocal total_hits
        total_hits += 1
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(evt, ensure_ascii=False) + "\n")
        kind = evt.get("kind")
        if kind in {"request", "response"}:
            print(
                f"[SNIFF][{kind.upper()}] {evt.get('method', '')} {evt.get('status', '')} {evt.get('url', '')}",
                flush=True,
            )

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
        _set_skip_debugger_pause(context, page)
        if "c168b2.cc" not in str(page.url or "").lower():
            try:
                page.goto("https://c168b2.cc/", wait_until="domcontentloaded", timeout=120_000)
            except Exception:
                pass
        # Ép chuyển sang Game B để user thao tác trong sảnh vendor.
        try:
            gameb = page.evaluate(_JS_OPEN_GAME_B_LOBBY, {"platformId": 1012, "gameId": 10120000})
        except Exception as e:
            return {"ok": False, "error": f"open_game_b_eval_failed:{e}"}
        if not isinstance(gameb, dict) or not gameb.get("ok"):
            return {"ok": False, "error": "open_game_b_failed", "detail": gameb}
        game_url = str(gameb.get("game_url") or "")
        if not game_url:
            return {"ok": False, "error": "empty_game_url"}
        try:
            page.goto(game_url, wait_until="domcontentloaded", timeout=120_000)
            page.wait_for_timeout(2000)
            page.bring_to_front()
        except Exception:
            pass

        def _bind_page(page) -> None:
            _set_skip_debugger_pause(context, page)
            try:
                cdp = context.new_cdp_session(page)
                cdp.send("Network.enable")
                cdp_sessions.append(cdp)
            except Exception:
                return

            def _on_request(params: dict[str, Any]) -> None:
                req = params.get("request") or {}
                rtype = str(params.get("type") or "")
                if rtype not in {"XHR", "Fetch"}:
                    return
                rid = str(params.get("requestId") or "")
                row = {
                    "ts": time.time(),
                    "kind": "request",
                    "requestId": rid,
                    "type": rtype,
                    "method": str(req.get("method") or ""),
                    "url": str(req.get("url") or ""),
                    "postData": _short(req.get("postData") or ""),
                }
                req_index[rid] = row
                _write(row)

            def _on_response(params: dict[str, Any]) -> None:
                resp = params.get("response") or {}
                rtype = str(params.get("type") or "")
                if rtype not in {"XHR", "Fetch"}:
                    return
                rid = str(params.get("requestId") or "")
                base = req_index.get(rid, {})
                row = {
                    "ts": time.time(),
                    "kind": "response",
                    "requestId": rid,
                    "type": rtype,
                    "method": base.get("method", ""),
                    "url": str(resp.get("url") or base.get("url") or ""),
                    "status": int(resp.get("status") or 0),
                }
                try:
                    body = cdp.send("Network.getResponseBody", {"requestId": rid})
                    if isinstance(body, dict):
                        row["body"] = _short(body.get("body") or "")
                        row["base64Encoded"] = bool(body.get("base64Encoded"))
                except Exception as e:
                    row["body_error"] = _short(str(e), 160)
                _write(row)

            cdp.on("Network.requestWillBeSent", _on_request)
            cdp.on("Network.responseReceived", _on_response)

        for pg in list(context.pages):
            _bind_page(pg)
        context.on("page", _bind_page)

        print(
            f"[SNIFF] User={user} | cdp={cdp_url}\n"
            f"[SNIFF] Đã chuyển sang Game B. Click tay vào bàn rồi đợi. Log: {out_path}",
            flush=True,
        )

        deadline = time.time() + max(30, int(duration_sec))
        while time.time() < deadline:
            time.sleep(0.5)

    return {
        "ok": True,
        "username": user,
        "duration_sec": int(duration_sec),
        "hits": total_hits,
        "log_file": str(out_path),
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    os.environ.setdefault("PYTHONUNBUFFERED", "1")

    ap = argparse.ArgumentParser(description="Sniff full XHR/Fetch qua CDP")
    ap.add_argument("--username", required=True, help="Username c168 trong allgame DB")
    ap.add_argument("--duration", type=int, default=180, help="Thời gian bắt log (giây)")
    args = ap.parse_args()

    out = run_sniff(args.username, duration_sec=args.duration)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

