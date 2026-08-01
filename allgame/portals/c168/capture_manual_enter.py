#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bắt thủ công thao tác vào bàn: log XHR + WS frame để tìm tín hiệu join table."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent
_REPO = _ROOT.parent.parent.parent
if str(_ROOT) in sys.path:
    sys.path.remove(str(_ROOT))
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from allgame.db.accounts_db import get_account
from allgame.config_util import load_config
from allgame.portals.c168.open_chrome_token import ensure_chrome_for_username
from allgame.vendor.config import vendor_table_cfg

_VENDOR_MARKERS = ("/player/webmain", "bpcdf.", "tgmeq", "mhuxu", "vesnamex", "intplaynet.com/player")

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
      body: JSON.stringify({ os_type: 3, callContext: "manual_capture_open_gameb", time: now }),
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

_JS_CLICK_LOBBY_TABLE = """
({ tableName, tableId }) => {
  const name = String(tableName || "").trim();
  const codeRe = new RegExp('(?:Baccarat\\\\s+)?' + name + '(?:\\\\s|$|\\\\n)', 'i');
  const hit = (doc) => {
    if (!doc) return null;
    const byAttr = doc.querySelector(
      `[data-table-id="${tableId}"],[data-tableid="${tableId}"],[data-id="${tableId}"]`
    );
    if (byAttr) {
      byAttr.click();
      return { ok: true, how: "attr" };
    }
    const picks = [];
    for (const el of doc.querySelectorAll('div,li,a,span,td,section,article')) {
      const t = (el.textContent || '').replace(/\\s+/g, ' ').trim();
      if (!codeRe.test(t)) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 24 || r.height < 18 || r.bottom < 0 || r.right < 0) continue;
      const area = r.width * r.height;
      if (area <= 0 || area > 160000) continue;
      picks.push({ el, area, label: t.slice(0, 120) });
    }
    picks.sort((a, b) => a.area - b.area);
    if (picks.length) {
      picks[0].el.click();
      return { ok: true, how: "text", label: picks[0].label };
    }
    return null;
  };
  let r = hit(document);
  if (r) return r;
  for (const fr of document.querySelectorAll("iframe")) {
    try {
      r = hit(fr.contentDocument);
      if (r) return { ...r, iframe: true };
    } catch (e) {}
  }
  return { ok: false, reason: "not_found" };
}
"""


def _now_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _short(v: Any, n: int = 280) -> str:
    s = str(v or "").replace("\n", " ").strip()
    return s[:n] + ("..." if len(s) > n else "")


def _extract_json_from_ws_payload(raw: Any) -> dict[str, Any] | None:
    txt = ""
    if isinstance(raw, (bytes, bytearray)):
        b = bytes(raw)
        i = b.find(b"{")
        if i >= 0:
            try:
                txt = b[i:].decode("utf-8", errors="ignore")
            except Exception:
                txt = ""
    else:
        txt = str(raw or "")
        i = txt.find("{")
        if i >= 0:
            txt = txt[i:]
    if not txt:
        return None
    j = txt.rfind("}")
    if j <= 0:
        return None
    txt = txt[: j + 1]
    try:
        obj = json.loads(txt)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def run_capture(
    username: str,
    duration_sec: int = 120,
    auto_click: bool = True,
    table_name: str = "C06",
    table_id: int = 1006,
) -> dict[str, Any]:
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
    out_file = logs_dir / f"manual_enter_{user}_{_now_tag()}.jsonl"

    req_hits = 0
    ws_hits = 0
    ws_table_hits = 0
    ws_open_hits = 0

    def _write(row: dict[str, Any]) -> None:
        with out_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        contexts = list(browser.contexts) if browser.contexts else [browser.new_context()]
        context = contexts[0]
        page = context.pages[0] if context.pages else context.new_page()
        # Ưu tiên context/page đang có c168/vendor để bám đúng cửa sổ user thao tác.
        for ctx in contexts:
            for pg in list(ctx.pages or []):
                u = str(pg.url or "").lower()
                if "c168b2.cc" in u or any(m in u for m in _VENDOR_MARKERS):
                    context = ctx
                    page = pg
                    break
        try:
            if "c168b2.cc" not in str(page.url or "").lower():
                page.goto("https://c168b2.cc/", wait_until="domcontentloaded", timeout=120_000)
            gameb = page.evaluate(_JS_OPEN_GAME_B_LOBBY, {"platformId": 1012, "gameId": 10120000})
            if not isinstance(gameb, dict) or not gameb.get("ok"):
                return {"ok": False, "error": "open_game_b_failed", "detail": gameb}
            game_url = str(gameb.get("game_url") or "")
            if not game_url:
                return {"ok": False, "error": "empty_game_url"}
            page.goto(game_url, wait_until="domcontentloaded", timeout=120_000)
            page.wait_for_timeout(2500)
            for pg in list(context.pages):
                u = str(pg.url or "").lower()
                if any(m in u for m in _VENDOR_MARKERS):
                    page = pg
                    break
        except Exception as e:
            return {"ok": False, "error": f"open_game_b_exception:{e}"}
        try:
            page.bring_to_front()
        except Exception:
            pass

        print(
            f"[CAPTURE] User={user} cdp={cdp_url}\n"
            f"[CAPTURE] Đã đưa vào sảnh Game B. Bắt đầu {duration_sec}s.\n"
            f"[CAPTURE] Log file: {out_file}",
            flush=True,
        )
        if auto_click:
            try:
                click_out = page.evaluate(
                    _JS_CLICK_LOBBY_TABLE,
                    {"tableName": str(table_name or "C06"), "tableId": int(table_id or 1006)},
                )
            except Exception as e:
                click_out = {"ok": False, "error": str(e)}
            _write({"ts": time.time(), "kind": "auto_click", "result": click_out})
            print(f"[CAPTURE] Auto click {table_name}/{table_id}: {click_out}", flush=True)
        else:
            print("[CAPTURE] Hãy click tay vào bàn ngay bây giờ.", flush=True)

        req_index: dict[str, dict[str, Any]] = {}
        ws_index: dict[str, str] = {}
        cdp_sessions = []
        seen_ctx_req: set[str] = set()

        def _bind_cdp(ctx, pg) -> None:
            nonlocal req_hits, ws_hits, ws_table_hits, ws_open_hits
            try:
                cdp = ctx.new_cdp_session(pg)
                cdp.send("Network.enable")
                cdp_sessions.append(cdp)
            except Exception:
                return

            def _on_req(params: dict[str, Any]) -> None:
                nonlocal req_hits
                req = params.get("request") or {}
                url = str(req.get("url") or "")
                rtype = str(params.get("type") or "")
                if "/player/" not in url and rtype not in {"XHR", "Fetch"}:
                    return
                rid = str(params.get("requestId") or "")
                req_hits += 1
                row = {
                    "ts": time.time(),
                    "kind": "http_request",
                    "requestId": rid,
                    "type": rtype,
                    "method": str(req.get("method") or ""),
                    "url": url,
                    "body": _short(req.get("postData") or ""),
                }
                req_index[rid] = row
                _write(row)

            def _on_resp(params: dict[str, Any]) -> None:
                resp = params.get("response") or {}
                rid = str(params.get("requestId") or "")
                base = req_index.get(rid, {})
                url = str(resp.get("url") or base.get("url") or "")
                if "/player/" not in url and str(base.get("type") or "") not in {"XHR", "Fetch"}:
                    return
                row = {
                    "ts": time.time(),
                    "kind": "http_response",
                    "requestId": rid,
                    "status": int(resp.get("status") or 0),
                    "method": base.get("method") or "",
                    "url": url,
                }
                try:
                    body = cdp.send("Network.getResponseBody", {"requestId": rid})
                    if isinstance(body, dict):
                        row["body"] = _short(body.get("body") or "")
                except Exception:
                    pass
                _write(row)

            def _on_ws_open(params: dict[str, Any]) -> None:
                nonlocal ws_open_hits
                wsid = str(params.get("requestId") or "")
                url = str((params.get("url") or ""))
                ws_open_hits += 1
                ws_index[wsid] = url
                _write({"ts": time.time(), "kind": "ws_open", "ws_url": url, "requestId": wsid})

            def _on_ws_frame(params: dict[str, Any], direction: str) -> None:
                nonlocal ws_hits, ws_table_hits
                wsid = str(params.get("requestId") or "")
                ws_url = ws_index.get(wsid, "")
                resp = params.get("response") or {}
                raw = resp.get("payloadData") or ""
                ws_hits += 1
                obj = _extract_json_from_ws_payload(raw)
                row = {
                    "ts": time.time(),
                    "kind": "ws_frame",
                    "direction": direction,
                    "requestId": wsid,
                    "ws_url": ws_url,
                    "raw": _short(raw),
                }
                if isinstance(obj, dict):
                    msg = obj.get("message")
                    row["messageType"] = str(obj.get("messageType") or "")
                    if isinstance(msg, dict):
                        table_id = msg.get("tableID")
                        if table_id is not None:
                            row["tableID"] = table_id
                            ws_table_hits += 1
                _write(row)

            cdp.on("Network.requestWillBeSent", _on_req)
            cdp.on("Network.responseReceived", _on_resp)
            cdp.on("Network.webSocketCreated", _on_ws_open)
            cdp.on("Network.webSocketFrameReceived", lambda p: _on_ws_frame(p, "recv"))
            cdp.on("Network.webSocketFrameSent", lambda p: _on_ws_frame(p, "sent"))

        def _on_ctx_request(req) -> None:
            nonlocal req_hits
            try:
                rid = str(req.timing.get("requestStart", "")) + "|" + str(req.url or "")
            except Exception:
                rid = str(req.url or "")
            if rid in seen_ctx_req:
                return
            seen_ctx_req.add(rid)
            u = str(req.url or "")
            if "/player/" not in u:
                return
            req_hits += 1
            body = ""
            try:
                body = req.post_data or ""
            except Exception:
                body = ""
            _write(
                {
                    "ts": time.time(),
                    "kind": "http_request_ctx",
                    "method": str(req.method or ""),
                    "url": u,
                    "body": _short(body),
                }
            )

        def _on_ctx_response(resp) -> None:
            u = str(resp.url or "")
            if "/player/" not in u:
                return
            txt = ""
            try:
                txt = resp.text()
            except Exception:
                txt = ""
            _write(
                {
                    "ts": time.time(),
                    "kind": "http_response_ctx",
                    "status": int(resp.status or 0),
                    "url": u,
                    "body": _short(txt),
                }
            )

        def _on_ctx_ws(ws) -> None:
            nonlocal ws_open_hits
            u = str(ws.url or "")
            ws_open_hits += 1
            _write({"ts": time.time(), "kind": "ws_open_ctx", "ws_url": u})

        _write({"ts": time.time(), "kind": "capture_start", "duration_sec": int(duration_sec), "username": user})
        for ctx in contexts:
            for pg in list(ctx.pages):
                _bind_cdp(ctx, pg)
            ctx.on("page", lambda pg, ctx=ctx: _bind_cdp(ctx, pg))
            ctx.on("request", _on_ctx_request)
            ctx.on("response", _on_ctx_response)
            ctx.on("websocket", _on_ctx_ws)

        deadline = time.time() + max(20, int(duration_sec))
        while time.time() < deadline:
            try:
                url_rows = []
                for ctx in contexts:
                    for pg in list(ctx.pages or []):
                        u = str(pg.url or "")
                        if u:
                            url_rows.append(u[:180])
                _write({"ts": time.time(), "kind": "pages_snapshot", "urls": url_rows[:12]})
            except Exception:
                pass
            time.sleep(0.4)

    return {
        "ok": True,
        "username": user,
        "duration_sec": int(duration_sec),
        "http_hits": req_hits,
        "ws_open_hits": ws_open_hits,
        "ws_hits": ws_hits,
        "ws_table_hits": ws_table_hits,
        "log_file": str(out_file),
        "table_name": str(table_name or ""),
        "table_id": int(table_id or 0),
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    os.environ.setdefault("PYTHONUNBUFFERED", "1")

    ap = argparse.ArgumentParser(description="Capture thao tác click tay vào bàn C168")
    ap.add_argument("--username", required=True, help="Username c168")
    ap.add_argument("--duration", type=int, default=120, help="Thời gian nghe log (giây)")
    ap.add_argument("--manual-click", action="store_true", help="Không auto click, tự click tay")
    ap.add_argument("--table-name", default="", help="Tên bàn (vd C06), mặc định lấy từ config")
    ap.add_argument("--table-id", type=int, default=0, help="ID bàn (vd 1005), mặc định lấy từ config")
    args = ap.parse_args()
    cfg = load_config()
    vc = vendor_table_cfg(cfg)
    tname = str(args.table_name or vc.get("table_name") or "C06")
    tid = int(args.table_id or vc.get("table_id") or 1006)

    out = run_capture(
        args.username,
        duration_sec=args.duration,
        auto_click=not args.manual_click,
        table_name=tname,
        table_id=tid,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

