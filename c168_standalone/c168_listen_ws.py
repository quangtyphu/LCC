# -*- coding: utf-8 -*-
"""
Lắng nghe WebSocket vendor C168 (h54uk) qua Chrome CDP — phiên / kết quả bàn.

Cần Chrome đang mở + vào Game B + bàn C06 (dùng post_open_chrome trước):

  python c168_post_open_chrome.py -u hoangnam47
  python c168_listen_ws.py -u hoangnam47 --table-id 1006

API:
  from c168_listen_ws import ListenParams, listen_vendor_ws
  listen_vendor_ws(ListenParams(username="hoangnam47", table_id=1006))
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from c168_mex_protocol import decode_mex_frame, extract_baccarat_event, table_id_from_obj
from c168_vendor_enter_table import DEFAULT_TABLE_ID, DEFAULT_TABLE_NAME
from c168_vendor_ws_sniff import BrowserCdpSniffer, adopt_or_create_sniffer, decode_frame

_DIR = Path(__file__).resolve().parent
LOG_DIR = _DIR / "capture_logs"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


@dataclass
class ListenParams:
    username: str = ""
    cdp_url: str = ""
    table_id: int = DEFAULT_TABLE_ID
    table_name: str = DEFAULT_TABLE_NAME
    duration_sec: int = 86400 * 7
    log_path: Path | None = None
    sniffer: BrowserCdpSniffer | None = None


@dataclass
class ListenResult:
    ok: bool
    exit_code: int
    cdp_url: str
    log_path: str
    table_id: int
    mode: str = "cdp"
    recv_frames: int = 0
    parsed_frames: int = 0
    table_messages: int = 0
    round_events: int = 0
    error: str = ""


def resolve_cdp_url(*, username: str = "", cdp_url: str = "") -> str:
    u = (cdp_url or "").strip().rstrip("/")
    if u:
        return u
    user = (username or "").strip()
    if not user:
        raise ValueError("Cần --username hoặc --cdp")
    from c168_chrome_session import resolve_chrome_session

    cms = resolve_chrome_session(username=user)
    if not cms:
        raise ValueError(f"Không tìm thấy acc C168: {user}")
    return cms.cdp_url.rstrip("/")


def _make_on_frame(
    *,
    params: ListenParams,
    tid_filter: int,
    log_f: Any,
) -> tuple[Callable[[str, str, str], None], dict[str, Any]]:
    from c168_vendor_auto_bet import _extract_event, _table_id_from_obj, winner_label

    state: dict[str, Any] = {
        "ws_frames": 0,
        "ws_parsed": 0,
        "ws_table_msgs": 0,
        "ws_table_hits": 0,
        "seen_table_ids": set(),
        "seen_round": set(),
        "table_focus_printed": False,
        "log_errors": 0,
        "last_status": 0.0,
    }

    def _log_line(payload: dict[str, Any]) -> None:
        try:
            log_f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            log_f.flush()
        except Exception as exc:
            state["log_errors"] += 1
            if state["log_errors"] == 1:
                print(f"[{_ts()}] Không ghi log: {exc}", flush=True)

    def on_frame(direction: str, url: str, data: str) -> None:
        if "h54uk" not in url.lower() or direction != "recv":
            return
        state["ws_frames"] += 1
        text = decode_frame(data)
        text2, obj = decode_mex_frame(text if isinstance(text, str) else data)
        if not isinstance(obj, dict):
            return
        state["ws_parsed"] += 1

        tid_seen = _table_id_from_obj(obj) or table_id_from_obj(obj)
        if tid_seen is not None:
            state["seen_table_ids"].add(int(tid_seen))
        mt = str(obj.get("messageType") or "")
        handler = obj.get("handler")
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        et = str(msg.get("eventType") or "")

        if tid_filter > 0 and tid_seen == tid_filter:
            state["ws_table_msgs"] += 1
            if not state["table_focus_printed"] and mt in ("GameInfo", "GameHallInfo"):
                state["table_focus_printed"] = True
                print(
                    f"[{_ts()}] ★ WS bàn {tid_filter} ({params.table_name}, {mt} h{handler})",
                    flush=True,
                )
            if et in ("GP_NEW_GAME_START", "GP_WINNER"):
                _log_line(
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "dir": direction,
                        "messageType": mt,
                        "handler": handler,
                        "eventType": et,
                        "tableID": tid_seen,
                        "gameRound": msg.get("gameRound"),
                        "gameShoe": msg.get("gameShoe"),
                        "winner": msg.get("winner"),
                    }
                )

        ev = _extract_event(obj, tid_filter) if tid_filter > 0 else None
        if not ev and tid_filter > 0:
            ev = extract_baccarat_event(obj, table_id=tid_filter)
        if not ev or ev.get("kind") not in ("round_start", "round_result"):
            return

        state["ws_table_hits"] += 1
        dedupe_key = (
            str(ev.get("kind")),
            int(ev.get("shoe") or 0),
            int(ev.get("round") or 0),
            tid_filter or tid_seen,
        )
        if dedupe_key in state["seen_round"]:
            return
        state["seen_round"].add(dedupe_key)
        if ev.get("kind") == "round_start":
            print(
                f"[{_ts()}] PHIEN MOI | bàn {tid_filter or tid_seen} "
                f"shoe {ev.get('shoe')} ván {ev.get('round')}",
                flush=True,
            )
        else:
            w = winner_label(
                ev.get("winner"),
                player_val=ev.get("player_val"),
                banker_val=ev.get("banker_val"),
            )
            extra = ""
            if ev.get("player_val") is not None and ev.get("banker_val") is not None:
                extra = f" (Con {ev['player_val']} — Cái {ev['banker_val']})"
            print(
                f"[{_ts()}] KET QUA PHIEN | bàn {tid_filter or tid_seen} "
                f"ván {ev.get('round')}: {w}{extra}",
                flush=True,
            )

    return on_frame, state


def _run_wait_loop(
    *,
    duration_s: int,
    tid_filter: int,
    table_name: str,
    state: dict[str, Any],
) -> None:
    """Chờ duration — chỉ in PHIEN MOI / KET QUA PHIEN từ on_frame."""
    t0 = time.time()
    while time.time() - t0 < duration_s:
        time.sleep(5)


def listen_vendor_ws(params: ListenParams) -> ListenResult:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    tid_filter = int(params.table_id or 0)
    duration_s = max(1, int(params.duration_sec or 300))
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = params.log_path or (LOG_DIR / f"listen_{stamp}.jsonl")

    user_lbl = params.username or params.cdp_url or "?"
    print(
        f"\n══ c168_listen_ws [cdp] | {user_lbl} | "
        f"bàn {params.table_name} tableID={tid_filter} ══",
        flush=True,
    )
    print(f"Log: {log_path}", flush=True)
    print("Ctrl+C dừng\n", flush=True)

    log_f = log_path.open("a", encoding="utf-8")
    on_frame, state = _make_on_frame(params=params, tid_filter=tid_filter, log_f=log_f)

    try:
        cdp_base = resolve_cdp_url(
            username=params.username, cdp_url=params.cdp_url
        )
    except ValueError as e:
        log_f.close()
        return ListenResult(
            ok=False,
            exit_code=1,
            cdp_url="",
            log_path=str(log_path),
            table_id=tid_filter,
            error=str(e),
        )

    print(f"CDP: {cdp_base}", flush=True)

    try:
        sniffer = adopt_or_create_sniffer(cdp_base, on_frame, existing=params.sniffer)
    except RuntimeError as e:
        log_f.close()
        return ListenResult(
            ok=False,
            exit_code=1,
            cdp_url=cdp_base,
            log_path=str(log_path),
            table_id=tid_filter,
            error=str(e),
        )

    try:
        sniffer.reinject_ws_hooks()
        sniffer._attach_existing_targets()
        sniffer.reset_h54uk_counters()
        time.sleep(0.6)
    except Exception:
        pass

    if not sniffer.wait_h54uk(20):
        print(
            "  Chưa thấy h54uk — mở Game B và vào bàn trong Chrome "
            "(python c168_post_open_chrome.py -u USER).",
            flush=True,
        )

    try:
        _run_wait_loop(
            duration_s=duration_s,
            tid_filter=tid_filter,
            table_name=params.table_name,
            state=state,
        )
    finally:
        sniffer.stop()
        log_f.close()

    ok = state["ws_parsed"] > 0 or sniffer.frame_count > 0
    print(
        f"\n=== Xong [cdp] | recv {state['ws_frames']} parse {state['ws_parsed']} | "
        f"phiên/KQ {state['ws_table_hits']} ===",
        flush=True,
    )
    return ListenResult(
        ok=ok,
        exit_code=0 if ok else 2,
        cdp_url=cdp_base,
        log_path=str(log_path),
        table_id=tid_filter,
        mode="cdp",
        recv_frames=state["ws_frames"],
        parsed_frames=state["ws_parsed"],
        table_messages=state["ws_table_msgs"],
        round_events=state["ws_table_hits"],
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Nghe WS vendor C168 qua Chrome CDP — PHIEN MOI / KET QUA PHIEN"
    )
    ap.add_argument("-u", "--username", default="", help="acc C168 (CDP theo profile)")
    ap.add_argument("--cdp", default="", help="CDP URL (vd http://127.0.0.1:9361)")
    ap.add_argument("--table", default=DEFAULT_TABLE_NAME)
    ap.add_argument("--table-id", type=int, default=DEFAULT_TABLE_ID)
    ap.add_argument("--sec", type=int, default=86400 * 7)
    ap.add_argument("--log", default="")
    args = ap.parse_args()
    log_path = Path(args.log) if args.log else None
    try:
        r = listen_vendor_ws(
            ListenParams(
                username=args.username.strip(),
                cdp_url=args.cdp.strip(),
                table_id=int(args.table_id),
                table_name=str(args.table).strip(),
                duration_sec=int(args.sec),
                log_path=log_path,
            )
        )
    except KeyboardInterrupt:
        print("\nDừng (Ctrl+C)", flush=True)
        return 130
    if r.error:
        print(r.error, file=sys.stderr)
    return r.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
