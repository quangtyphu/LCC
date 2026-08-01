# -*- coding: utf-8 -*-
"""WS reporter: 1 account — PHIEN MOI / KET QUA (reuse sniffer c168_listen_ws)."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from allgame.config_util import load_config
from allgame.vendor.c168_auto_bet import C168AutoBettor
from allgame.vendor.c168_keepalive import tick_table_keepalive
from allgame.vendor.c168_vendor_flow import attach_c168_ws_listener, get_listen_sniffer, make_listen_on_frame
from allgame.vendor.config import vendor_table_cfg


@dataclass
class _ReporterState:
    key: str = ""
    stop_event: threading.Event | None = None
    thread: threading.Thread | None = None


_state = _ReporterState()
_lock = threading.Lock()


def stop_reporter() -> None:
    with _lock:
        ev = _state.stop_event
        th = _state.thread
        _state.key = ""
        _state.stop_event = None
        _state.thread = None
    if ev:
        ev.set()
    if th and th.is_alive():
        th.join(timeout=2.0)
    # Không stop CDP sniffer — connect/reconnect cần giữ hook WS.


def ensure_reporter(
    *,
    candidate_key: str,
    portal_id: str,
    username: str,
    cdp_url: str,
) -> None:
    with _lock:
        if _state.key == candidate_key and _state.thread and _state.thread.is_alive():
            return
        old_ev = _state.stop_event
        old_th = _state.thread
        ev = threading.Event()
        th = threading.Thread(
            target=_run_reporter,
            args=(candidate_key, portal_id, username, cdp_url, ev),
            daemon=True,
            name=f"ws-reporter-{candidate_key}",
        )
        _state.key = candidate_key
        _state.stop_event = ev
        _state.thread = th
    if old_ev:
        old_ev.set()
    if old_th and old_th.is_alive():
        old_th.join(timeout=1.5)
    th.start()


def _run_reporter(
    key: str,
    portal_id: str,
    username: str,
    cdp_url: str,
    stop_event: threading.Event,
) -> None:
    pid = str(portal_id or "").strip().lower()
    if pid == "c168":
        _run_c168_reporter(username, cdp_url, stop_event)
        return
    _run_playwright_reporter(portal_id, username, cdp_url, stop_event)


def _run_c168_reporter(username: str, cdp_url: str, stop_event: threading.Event) -> None:
    cfg = load_config()
    vc = vendor_table_cfg(cfg)
    table_name = str(vc.get("table_name") or "C06")
    table_id = int(vc.get("table_id") or 1006)
    base = cdp_url.rstrip("/")

    bettor: C168AutoBettor | None = None
    if vc.get("auto_bet_enabled"):
        bettor = C168AutoBettor(
            cdp_url=base,
            table_id=table_id,
            stake_min=int(vc.get("stake_min") or 10),
            stake_max=int(vc.get("stake_max") or 20),
            stake_unit=str(vc.get("stake_unit") or "k"),
            stake_step=int(vc.get("stake_step") or 10),
            bet_limit_id=int(vc.get("bet_limit_id") or 851101),
            max_rounds=int(vc.get("max_bet_rounds") or 0),
            enabled=True,
        )

    def _on_round(kind: str, ev: dict[str, Any]) -> None:
        if bettor is not None:
            bettor.on_round(kind, ev)

    ws_state: dict[str, Any] = {"last_ws_recv": 0.0}
    base_on_frame, _ = make_listen_on_frame(
        table_id=table_id,
        table_name=table_name,
        log_prefix="[ALLGAME][WS]",
        state=ws_state,
        on_round=_on_round,
    )

    def on_frame(direction: str, url: str, data: str) -> None:
        base_on_frame(direction, url, data)
        if direction == "recv" and "h54uk" in str(url or "").lower():
            ws_state["last_ws_recv"] = time.time()

    try:
        existing = get_listen_sniffer(base)
        attach_c168_ws_listener(
            base,
            table_id=table_id,
            table_name=table_name,
            log_prefix="[ALLGAME][WS]",
            on_frame=on_frame,
            existing_sniffer=existing,
        )
        bet_lbl = "BẬT" if bettor else "TẮT"
        print(
            f"[ALLGAME][WS] Reporter online (CDP/mex): c168-{username} | "
            f"bàn {table_name} id={table_id} | auto_bet={bet_lbl} | "
            f"keepalive=ON | sniffer={'reuse' if existing else 'attach'}",
            flush=True,
        )
        from allgame.vendor.c168_vendor_flow import _import_standalone

        _import_standalone()
        from c168_vendor_keepalive import inject_anti_idle_all  # type: ignore

        inject_anti_idle_all(base)

        keepalive_state: dict[str, Any] = {}
        last_refresh = time.time()
        last_keepalive = 0.0
        maintain_sec = float(vc.get("keepalive_maintain_sec") or 90)
        anti_idle_sec = float(vc.get("keepalive_anti_idle_sec") or 300)
        while not stop_event.is_set():
            now = time.time()
            if now - last_keepalive >= 20.0:
                last_keepalive = now
                keepalive_state["last_ws_recv"] = ws_state.get("last_ws_recv") or 0
                try:
                    tick_table_keepalive(
                        cdp_url=base,
                        table_id=table_id,
                        table_name=table_name,
                        state=keepalive_state,
                        maintain_interval_sec=maintain_sec,
                        anti_idle_interval_sec=anti_idle_sec,
                    )
                except Exception as exc:
                    print(f"[ALLGAME][KEEP] lỗi: {exc}", flush=True)
            if now - last_refresh > 45.0:
                try:
                    _import_standalone()
                    from c168_vendor_ws_sniff import refresh_sniffer_targets  # type: ignore

                    refresh_sniffer_targets()
                    sn = get_listen_sniffer(base)
                    if sn is not None:
                        sn.reinject_ws_hooks()
                        sn._attach_existing_targets()
                except Exception:
                    pass
                last_refresh = now
            time.sleep(0.5)
    except Exception as e:
        print(f"[ALLGAME][WS] reporter lỗi c168-{username}: {e}", flush=True)


def _run_playwright_reporter(
    portal_id: str,
    username: str,
    cdp_url: str,
    stop_event: threading.Event,
) -> None:
    import json
    import re

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"[ALLGAME][WS] reporter lỗi playwright: {e}", flush=True)
        return

    _GP_START_RE = re.compile(r"GP_NEW_GAME_START", re.I)
    _GP_RESULT_RE = re.compile(r"GP_WINNER|GP_RESULT", re.I)

    last_start = ""
    last_result = ""

    def _on_text(text: str) -> None:
        nonlocal last_start, last_result
        if not text:
            return
        raw = text
        try:
            raw = json.dumps(json.loads(text), ensure_ascii=False)
        except Exception:
            pass
        if _GP_START_RE.search(raw):
            sig = raw[:120]
            if sig != last_start:
                last_start = sig
                print(f"[ALLGAME][WS] Bắt đầu phiên | {portal_id}-{username}", flush=True)
        if _GP_RESULT_RE.search(raw):
            sig = raw[:120]
            if sig != last_result:
                last_result = sig
                print(f"[ALLGAME][WS] Kết quả | {portal_id}-{username}", flush=True)

    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()

            def _bind(page) -> None:
                try:
                    page.on(
                        "websocket",
                        lambda ws: ws.on(
                            "framereceived",
                            lambda payload: _on_text(
                                str(getattr(payload, "payload", payload) or "")
                            ),
                        ),
                    )
                except Exception:
                    pass

            for pg in context.pages:
                _bind(pg)
            context.on("page", _bind)
            print(f"[ALLGAME][WS] Reporter online: {portal_id}-{username}", flush=True)
            while not stop_event.is_set():
                time.sleep(0.5)
    except Exception as e:
        print(f"[ALLGAME][WS] reporter lỗi {portal_id}-{username}: {e}", flush=True)
