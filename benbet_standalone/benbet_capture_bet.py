# -*- coding: utf-8 -*-
"""
In HET log WebSocket (giong benbet_taixiu_ws listen) + ghi file.
Dat cuoc tren web (cung tai khoan) — xem terminal co betSuccess / frame la.

  python benbet_capture_bet.py -u USER -p PASS
  python benbet_capture_bet.py -u USER -p PASS --test-bet --side tai --amount 10000
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from benbet_taixiu_ws import (
    MIN_BET,
    TaiXiuWsClient,
    _hub_messages,
    _parse_signalr_payload,
    client_from_login,
    side_from_name,
)

LOG_DIR = Path(__file__).resolve().parent / "capture_logs"
PROFILE_JSON = LOG_DIR / "bet_profile.json"

BET_HINTS = (
    "bet",
    "BET",
    "betSuccess",
    "winResult",
    "betOfAccount",
    "BET_OF",
    "BET_SUCCESS",
)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _fix_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _append(log_path: Path, row: dict[str, Any]) -> None:
    row["t"] = time.time()
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _highlight(line: str) -> bool:
    low = line.lower()
    return any(h.lower() in low for h in BET_HINTS)


class CaptureWsClient(TaiXiuWsClient):
    """In tat ca WS + ghi jsonl."""

    def __init__(self, *args: Any, log_path: Path, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.log_path = log_path

    def _log(self, direction: str, text: str, *, hub: str = "", method: str = "") -> None:
        row = {"dir": direction, "payload": text, "hub": hub, "method": method}
        _append(self.log_path, row)
        prefix = ">>" if direction == "ws_send" else "<<"
        line = f"{prefix} {method or direction} {text[:800]}"
        if _highlight(text) or _highlight(method):
            print("\n*** " + line[:900] + " ***\n", flush=True)
        else:
            print(line, flush=True)

    def _send_hub(self, method: str, args: list[Any]) -> None:
        from benbet_game import HUB_TAI_XIU

        if not self._ws:
            raise RuntimeError("WebSocket chua ket noi")
        body = {"M": method, "A": args, "H": HUB_TAI_XIU, "I": self._next_id()}
        text = json.dumps(body, separators=(",", ":"))
        self._ws.send(text)
        self._log("ws_send", text, method=method)

    def _on_message(self, ws: Any, message: str | bytes) -> None:
        if isinstance(message, bytes):
            message = message.decode("utf-8", errors="replace")
        for part in message.split("\x1e"):
            part = part.strip()
            if not part:
                continue
            obj = _parse_signalr_payload(part)
            if not obj:
                if part.startswith("{"):
                    self._log("ws_recv_raw", part[:500])
                continue
            if obj.get("I") is not None and not obj.get("M"):
                pong = json.dumps({"I": obj["I"]})
                self._ws.send(pong)
                self._log("ws_send", pong, method="ping")
                continue
            for m in _hub_messages(obj):
                name = str(m.get("M") or "")
                args = m.get("A") or []
                payload = json.dumps(args, ensure_ascii=False)
                self._log("ws_recv", payload, method=name)

                if name in ("SESSION_INFO", "sessionInfo") and args:
                    info = args[0] if isinstance(args[0], dict) else {"raw": args}
                    self.session_info = info
                elif name in ("BET_SUCCESS", "betSuccess"):
                    self._bet_ok.set()
                elif name in ("MESSAGE", "message") and args:
                    msg = args[0] if args else args
                    if isinstance(msg, dict):
                        txt = msg.get("Description") or msg.get("Message") or str(msg)
                    else:
                        txt = str(msg)
                    if self._bet_err is None:
                        self._bet_err = txt


def extract_bet_profile(rows: list[dict]) -> dict[str, Any]:
    profile: dict[str, Any] = {
        "hub": "luckydiceHub",
        "bets_sent": [],
        "bet_responses": [],
    }
    for r in rows:
        p = str(r.get("payload") or "")
        m = r.get("method") or ""
        if r.get("dir") == "ws_send" and (
            m in ("BET", "Bet") or '"M":"Bet"' in p or '"M":"BET"' in p
        ):
            obj = _parse_signalr_payload(p)
            if obj:
                profile["bets_sent"].append(obj)
        if m in ("betSuccess", "BET_SUCCESS", "betOfAccount", "BET_OF_ACCOUNT"):
            profile["bet_responses"].append({"method": m, "payload": p})
        if "bet" in m.lower() or "bet" in p.lower():
            if m not in ("sessionInfo", "SESSION_INFO"):
                profile["bet_responses"].append({"method": m, "payload": p[:500]})
    return profile


def _analyze(log_path: Path) -> tuple[str, dict[str, Any]]:
    rows: list[dict] = []
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    profile = extract_bet_profile(rows)
    sent = [r for r in rows if r.get("dir") == "ws_send"]
    recv = [r for r in rows if r.get("dir") == "ws_recv"]

    lines = [
        f"Log: {log_path}",
        f"Tong dong: {len(rows)} | gui: {len(sent)} | nhan: {len(recv)}",
        "",
        "=== Lenh BET gui (tu script --test-bet hoac bat duoc) ===",
    ]
    if profile["bets_sent"]:
        for x in profile["bets_sent"]:
            lines.append(f"  {json.dumps(x, ensure_ascii=False)}")
    else:
        lines.append("  (chua co frame gui BET)")

    lines.append("")
    lines.append("=== Phan hoi lien quan cuoc (betSuccess, betOfAccount, ...) ===")
    interesting = [
        r
        for r in recv
        if _highlight(str(r.get("method", ""))) or _highlight(str(r.get("payload", "")))
    ]
    if interesting:
        for r in interesting[-30:]:
            lines.append(f"  << {r.get('method')} {str(r.get('payload', ''))[:600]}")
    else:
        lines.append("  (chua thay — dat cuoc tren web khi script dang chay, hoac dung --test-bet)")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_JSON.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    lines.append(f"\nProfile: {PROFILE_JSON}")
    text = "\n".join(lines)
    log_path.with_suffix(".analysis.txt").write_text(text, encoding="utf-8")
    return text, profile


def run_capture(
    username: str,
    password: str,
    *,
    test_bet: bool = False,
    side: str = "tai",
    amount: int = MIN_BET,
) -> int:
    _fix_stdout()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"ws_full_{username}_{_ts()}.jsonl"
    log_path.write_text("", encoding="utf-8")

    print("=" * 60, flush=True)
    print("LOG WS DAY DU — taixiu.3dbenbet.net / luckydiceHub", flush=True)
    print("=" * 60, flush=True)
    print(f"File: {log_path}\n", flush=True)

    try:
        base = client_from_login(username, password)
        client = CaptureWsClient(
            ws_url=base.ws_url,
            access_token=base.access_token,
            session_token=base.session_token,
            username=base.username,
            device_type=base.device_type,
            log_path=log_path,
        )
    except Exception as exc:
        print(f"Loi ket noi: {exc}")
        return 1

    stop = threading.Event()

    def wait_enter():
        try:
            input("\n>>> Nhan ENTER de dung va phan tich (Ctrl+C cung duoc)...\n")
        except EOFError:
            pass
        stop.set()

    threading.Thread(target=wait_enter, daemon=True).start()

    def run_ws():
        client.connect(block=True)

    t = threading.Thread(target=run_ws, daemon=True)
    t.start()
    time.sleep(2)

    print(
        "1. Giu terminal nay — moi dong << sessionInfo ... la WS (nhu listen)\n"
        "2. Mo game tren Chrome, DANG NHAP CUNG TK, vao Tai Xiu, dat cuoc\n"
        "3. Tim dong *** ... betSuccess / betOfAccount ... ***\n"
        "4. Hoac thu lenh tu script (chac chan co frame BET):\n"
        f"     Ctrl+C roi: python benbet_capture_bet.py -u {username} -p *** --test-bet --side {side} --amount {amount}\n",
        flush=True,
    )

    if test_bet:
        time.sleep(1)
        info = client.session_info or {}
        st = info.get("CurrentState")
        sid = info.get("SessionID")
        print(f"Phien SessionID={sid} CurrentState={st} (0=duoc cuoc)", flush=True)
        if st is not None and st != 0:
            print("Canh bao: khong phai phase dat cuoc — van thu gui BET...", flush=True)
        try:
            client.bet(side_from_name(side), amount)
            print("Da gui BET — doi 8s betSuccess...", flush=True)
            time.sleep(8)
        except Exception as exc:
            print(f"Gui BET loi: {exc}", flush=True)
        stop.set()

    while not stop.is_set() and t.is_alive():
        time.sleep(0.3)

    client.close()
    time.sleep(0.5)

    print("\n" + "=" * 60, flush=True)
    report, profile = _analyze(log_path)
    print(report, flush=True)

    if not profile.get("bets_sent") and not profile.get("bet_responses"):
        print(
            "\nGoi y: Frame BET khi dat tay nam tren WS cua TRINH DUYET, "
            "script Python la ket noi rieng — dat cuoc tren web se hien betSuccess o day "
            "neu server push. De thay chinh xac lenh gui: chay lai voi --test-bet.",
            flush=True,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    _fix_stdout()
    ap = argparse.ArgumentParser(description="Log WS day du + bat lenh cuoc")
    ap.add_argument("-u", "--username", default="longmebaihai")
    ap.add_argument("-p", "--password", default="Valentine1")
    ap.add_argument(
        "--test-bet",
        action="store_true",
        help="Tu gui 1 lenh BET de in frame mau (khong can Chrome)",
    )
    ap.add_argument("--side", default="tai", help="tai|xiu — dung voi --test-bet")
    ap.add_argument("--amount", type=int, default=MIN_BET)
    args = ap.parse_args(argv)
    return run_capture(
        args.username,
        args.password,
        test_bet=args.test_bet,
        side=args.side,
        amount=args.amount,
    )


if __name__ == "__main__":
    raise SystemExit(main())
