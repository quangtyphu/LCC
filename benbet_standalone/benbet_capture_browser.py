# -*- coding: utf-8 -*-
"""
Mo Chrome + bat lenh BET khi dat cuoc tay.

  python benbet_capture_browser.py -u USER -p PASS --fresh-chrome
  python benbet_diag_cdp.py
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

from benbet_capture_bet import LOG_DIR, _analyze, _fix_stdout, _highlight, _ts
from benbet_cdp_ws import CdpWsSniffer
from benbet_chrome import CDP_URL, cdp_is_alive, open_cdp_tab, start_chrome
from benbet_game import open_tai_xiu_session

_rows: list[dict[str, Any]] = []
_log_path: Path | None = None

BET_WHERE = """
=== LENH BET NAM O DAU? ===

  Trang play.3dbenbet.net  (Cocos)  --chi hien thi-->
       |
       |  HTTPS GET signalr/negotiate
       v
  wss://taixiu.3dbenbet.net/signalr/connect?...luckydiceHub...
       |
       |  Frame GUI (SignalR):
       |    {"M":"Bet","A":[so_tien, 0|1, 1],"H":"luckydiceHub","I":...}
       v
  Server game

  --> KHONG nam tren play.3dbenbet.net trong Network (XHR).
  --> Phai mo WS host: taixiu.3dbenbet.net (F12 → Network → WS).

Script CDP co the van = 0 frame neu Chrome chua gan duoc tab game.
"""


def _append_frame(direction: str, url: str, payload: str) -> None:
    global _rows, _log_path
    row: dict[str, Any] = {
        "dir": direction,
        "ws_url": url,
        "payload": payload,
        "method": "",
    }
    if '"M":"Bet"' in payload or '"M": "Bet"' in payload:
        row["method"] = "Bet"
    elif '"M":"BET"' in payload:
        row["method"] = "BET"
    for part in payload.split("\x1e"):
        part = part.strip()
        if not part.startswith("{"):
            continue
        try:
            obj = json.loads(part)
        except json.JSONDecodeError:
            continue
        msgs = obj.get("M")
        if isinstance(msgs, list) and msgs and isinstance(msgs[0], dict):
            row["method"] = str(msgs[0].get("M") or row["method"])
        elif isinstance(obj.get("M"), str):
            row["method"] = str(obj["M"])

    _rows.append(row)
    if _log_path:
        row_copy = dict(row)
        row_copy["t"] = time.time()
        with _log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row_copy, ensure_ascii=False) + "\n")

    prefix = ">>" if direction == "ws_send" else "<<"
    line = f"{prefix} [{row['method'] or 'ws'}] {payload[:900]}"
    if _highlight(payload) or _highlight(row["method"]):
        print("\n*** " + line + " ***\n", flush=True)
    else:
        print(line[:1000], flush=True)


def run_browser_capture(username: str, password: str, *, fresh_chrome: bool = False) -> int:
    global _rows, _log_path
    _fix_stdout()
    _rows = []
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _log_path = LOG_DIR / f"browser_ws_{username}_{_ts()}.jsonl"
    _log_path.write_text("", encoding="utf-8")

    print(BET_WHERE, flush=True)

    print("Dang login + lay launch URL...", flush=True)
    sess = open_tai_xiu_session(username, password)
    if not sess.get("ok"):
        print(f"Login that bai: {sess.get('login', {}).get('message')}")
        return 1
    launch_url = sess["launch_url"]

    if fresh_chrome or not cdp_is_alive():
        print("Mo Chrome moi (CDP 9223)...", flush=True)
        ok, msg = start_chrome(launch_url)
        if not ok:
            print(f"Khong mo duoc Chrome: {msg}")
            return 1
        print(f"Chrome: {msg}", flush=True)
        time.sleep(2)
    else:
        open_cdp_tab(launch_url)

    sniffer = CdpWsSniffer(CDP_URL, _append_frame)
    sniffer.start()

    print("Cho CDP gan vao tab game (toi da 90s)...", flush=True)
    if not sniffer.wait_ready(90):
        print(
            "\n[Canh bao] CDP chua bat duoc WS — van co the dat cuoc, "
            "nhung phai xem F12 thu cong (xem huong dan tren).\n",
            flush=True,
        )

    print("=" * 60, flush=True)
    print("DAT CUOC TAY → xem terminal hoac F12", flush=True)
    print("=" * 60, flush=True)
    print(
        "1. Vao TAI XIU CAN BANG\n"
        "2. F5 neu vua mo Chrome\n"
        "3. Dat 1 cuoc\n"
        "4. F12 → Network → loc 'taixiu' → WS → Messages (frame gui)\n"
        f"5. Log file: {_log_path}\n",
        flush=True,
    )

    stop = threading.Event()

    def wait_enter():
        try:
            input("\n>>> Xong — ENTER de phan tich...\n")
        except EOFError:
            time.sleep(600)
        stop.set()

    threading.Thread(target=wait_enter, daemon=True).start()
    while not stop.is_set():
        time.sleep(0.5)

    sniffer.stop()

    print("\n" + "=" * 60, flush=True)
    report, profile = _analyze(_log_path)
    print(report, flush=True)
    print(f"\nTong frame CDP: {sniffer.frame_count} | log rows: {len(_rows)}", flush=True)

    if not profile.get("bets_sent"):
        print(
            "\n--- Cach lay lenh BET bang tay (chac chan) ---\n"
            "F12 → Network → o loc go 'taixiu' → click dong WS\n"
            "  wss://taixiu.3dbenbet.net/signalr/connect...\n"
            "Tab Messages → tim frame gui co chu \"BET\" → copy JSON\n"
            "Gui lai chat de so sanh voi script Python.\n",
            flush=True,
        )
    return 0 if profile.get("bets_sent") else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Chrome + CDP bat lenh cuoc")
    ap.add_argument("-u", "--username", default="longmebaihai")
    ap.add_argument("-p", "--password", default="Valentine1")
    ap.add_argument("--fresh-chrome", action="store_true")
    args = ap.parse_args(argv)
    return run_browser_capture(args.username, args.password, fresh_chrome=args.fresh_chrome)


if __name__ == "__main__":
    raise SystemExit(main())
