#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Nghe WS bàn liên tục: in Bắt đầu phiên / Kết quả phiên realtime."""

from __future__ import annotations

import argparse
import json
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

from allgame.config_util import load_config
from allgame.db.accounts_db import get_account
from allgame.portals.c168.open_chrome_token import ensure_chrome_for_username
from allgame.vendor.ws_connector import connect_vendor_ws


def _extract_json(payload: Any) -> dict[str, Any] | None:
    txt = str(payload or "")
    i = txt.find("{")
    j = txt.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        obj = json.loads(txt[i : j + 1])
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def run_listen(
    username: str,
    table_name: str = "C06",
    table_id: int = 1006,
    max_seconds: int = 0,
) -> dict[str, Any]:
    user = str(username or "").strip()
    if not user:
        return {"ok": False, "error": "missing_username"}

    account = get_account("c168", user)
    if not account:
        return {"ok": False, "error": "account_not_found", "username": user}

    chrome = ensure_chrome_for_username(user, account=account)
    if not chrome.get("ok"):
        return {"ok": False, "error": "ensure_chrome_failed", "detail": chrome}

    cfg = load_config()
    vendor = dict(cfg.get("vendor") or {})
    vendor["table_name"] = str(table_name or "C06")
    vendor["table_id"] = int(table_id or 1006)
    cfg["vendor"] = vendor

    # Bước vào bàn/ws trước khi nghe realtime (retry vì vendor có thể hụt nhịp lần đầu).
    pre: dict[str, Any] = {"ok": False}
    for i in range(3):
        print(f"[WS] Bootstrap vào bàn... lần {i+1}/3", flush=True)
        pre_try = connect_vendor_ws(account, chrome=chrome, cfg=cfg)
        pre = pre_try if isinstance(pre_try, dict) else {"ok": False, "error": "bad_result_type"}
        if pre.get("ready_to_bet"):
            print("[WS] Bootstrap OK, bắt đầu nghe WS realtime", flush=True)
            break
        print(f"[WS] Bootstrap fail: {pre.get('enter_table_method') or pre.get('error') or 'unknown'}", flush=True)
        if i < 2:
            time.sleep(1.0)
    if not pre.get("ready_to_bet"):
        return {"ok": False, "error": "enter_table_failed", "detail": pre}

    cdp_url = str(chrome.get("cdp_url") or "")
    if not cdp_url:
        return {"ok": False, "error": "missing_cdp_url"}

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return {"ok": False, "error": "playwright_not_installed"}

    print(
        f"[WS] User={user} | table={table_name}/{table_id} | cdp={cdp_url}\n"
        "[WS] Đang nghe liên tục. Ctrl+C để dừng.",
        flush=True,
    )

    seen_new: set[tuple[int, int]] = set()
    seen_result: set[tuple[int, int]] = set()
    seen_streaming: set[str] = set()
    seen_sent_cmd: set[str] = set()
    started = time.time()

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        ws_map: dict[str, str] = {}
        cdp_sessions = []

        def _handle_payload(raw_payload: Any) -> None:
            obj = _extract_json(raw_payload)
            if not isinstance(obj, dict):
                return
            router = str(obj.get("router") or "")
            if router == "streamingInfo":
                tname = str(obj.get("tableId") or "")
                sname = str(obj.get("streamingName") or "")
                if tname.lower() == str(table_name).lower() or sname.lower().endswith(str(table_name).lower().replace("c", "")):
                    key = f"{tname}|{sname}|{obj.get('streamingUrl')}"
                    if key not in seen_streaming:
                        seen_streaming.add(key)
                        print(
                            f"[{_ts()}] StreamingInfo | table={tname} stream={sname} url={obj.get('streamingUrl')}",
                            flush=True,
                        )
            msg = obj.get("message")
            if not isinstance(msg, dict):
                return
            try:
                t_id = int(msg.get("tableID") or 0)
            except Exception:
                t_id = 0
            if t_id != int(table_id):
                return
            try:
                shoe = int(msg.get("gameShoe") or 0)
            except Exception:
                shoe = 0
            try:
                rnd = int(msg.get("gameRound") or 0)
            except Exception:
                rnd = 0
            ev = str(msg.get("eventType") or "")
            if ev == "GP_NEW_GAME_START":
                key = (shoe, rnd)
                if key not in seen_new:
                    seen_new.add(key)
                    print(f"[{_ts()}] Bắt đầu phiên | shoe={shoe} round={rnd} table={t_id}", flush=True)
            elif ev in {"GP_WINNER", "GP_RESULT"}:
                key = (shoe, rnd)
                if key not in seen_result:
                    seen_result.add(key)
                    print(f"[{_ts()}] Kết quả phiên   | shoe={shoe} round={rnd} table={t_id}", flush=True)

        def _handle_sent_payload(raw_payload: Any) -> None:
            txt = str(raw_payload or "")
            # In ra lệnh gửi đi có tín hiệu subscribe/join table.
            low = txt.lower()
            interesting = any(
                k in low
                for k in (
                    "tableid",
                    "querytableid",
                    "singletable",
                    "choose",
                    "subscribe",
                    "streaminginfo",
                    "btcb",
                )
            )
            if not interesting:
                return
            key = txt[:220]
            if key in seen_sent_cmd:
                return
            seen_sent_cmd.add(key)
            print(f"[{_ts()}] WS SENT cmd   | {txt[:260]}", flush=True)

        def _bind_page_cdp(pg) -> None:
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

            def _on_ws_frame_received(params: dict[str, Any]) -> None:
                rid = str(params.get("requestId") or "")
                ws_url = str(ws_map.get(rid) or "")
                resp = params.get("response") or {}
                raw_payload = resp.get("payloadData") or ""
                # WS có thể đã mở trước lúc bind -> không có webSocketCreated để map URL.
                # Khi đó vẫn parse payload theo tableID/eventType để không bỏ sót frame bàn.
                if ws_url and "h54uk" not in ws_url.lower():
                    return
                _handle_payload(raw_payload)

            def _on_ws_frame_sent(params: dict[str, Any]) -> None:
                rid = str(params.get("requestId") or "")
                ws_url = str(ws_map.get(rid) or "")
                if ws_url and "h54uk" not in ws_url.lower():
                    return
                resp = params.get("response") or {}
                raw_payload = resp.get("payloadData") or ""
                _handle_sent_payload(raw_payload)

            cdp.on("Network.webSocketCreated", _on_ws_created)
            cdp.on("Network.webSocketFrameReceived", _on_ws_frame_received)
            cdp.on("Network.webSocketFrameSent", _on_ws_frame_sent)

        for pg in list(context.pages or []):
            _bind_page_cdp(pg)
        context.on("page", _bind_page_cdp)

        try:
            last_ping = 0.0
            while True:
                if max_seconds > 0 and (time.time() - started) >= max_seconds:
                    break
                now = time.time()
                if now - last_ping >= 10:
                    last_ping = now
                    print(
                        f"[{_ts()}] listening... new_round={len(seen_new)} result={len(seen_result)} streaming={len(seen_streaming)} sent_cmd={len(seen_sent_cmd)}",
                        flush=True,
                    )
                time.sleep(0.4)
        except KeyboardInterrupt:
            pass

    return {
        "ok": True,
        "username": user,
        "table_name": table_name,
        "table_id": int(table_id),
        "new_round_events": len(seen_new),
        "result_events": len(seen_result),
        "streaming_info_events": len(seen_streaming),
        "sent_command_events": len(seen_sent_cmd),
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    os.environ.setdefault("PYTHONUNBUFFERED", "1")

    ap = argparse.ArgumentParser(description="Nghe WS bàn liên tục (bắt đầu phiên/kết quả phiên)")
    ap.add_argument("--username", required=True, help="Username c168")
    ap.add_argument("--table-name", default="C06", help="Tên bàn (mặc định C06)")
    ap.add_argument("--table-id", type=int, default=1006, help="ID bàn (mặc định 1006)")
    ap.add_argument("--max-seconds", type=int, default=0, help="0 = không giới hạn thời gian")
    args = ap.parse_args()

    out = run_listen(
        args.username,
        table_name=args.table_name,
        table_id=int(args.table_id),
        max_seconds=int(args.max_seconds),
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

