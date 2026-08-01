# -*- coding: utf-8 -*-
"""Thread watcher — gọi reconcile định kỳ."""

from __future__ import annotations

import threading
import time
from typing import Callable

from allgame.config_util import load_config
from allgame.orchestrator.reconcile import reconcile_once
from allgame.orchestrator.session_registry import get_registry
from allgame.vendor.ws_reporter import ensure_reporter, stop_reporter

_stop = threading.Event()


def _key_label(session_key: str) -> str:
    s = str(session_key or "").strip()
    if ":" not in s:
        return s or "unknown"
    portal, username = s.split(":", 1)
    return f"{portal.strip()} - {username.strip()}"


def request_stop() -> None:
    _stop.set()


def stopping() -> bool:
    return _stop.is_set()


def run_watcher(
    *,
    interval_sec: float | None = None,
    on_tick: Callable[[dict], None] | None = None,
) -> None:
    cfg = load_config()
    interval = float(
        interval_sec if interval_sec is not None else cfg.get("reconcile_interval_sec") or 60
    )
    print(f"[ALLGAME] Watcher reconcile mỗi {interval}s", flush=True)

    while not stopping():
        try:
            report = reconcile_once()
            opened = list(report.get("opened") or [])
            closed = list(report.get("closed") or [])
            skipped = list(report.get("skipped") or [])
            if on_tick:
                on_tick(report)
            elif opened or closed or skipped:
                print(
                    f"[ALLGAME] reconcile tick: target={report.get('target_count')} "
                    f"active={report.get('active_count')} "
                    f"opened={len(opened)} closed={len(closed)} skipped={len(skipped)}",
                    flush=True,
                )
                if opened:
                    opened_map = {
                        str(d.get("session_key")): d
                        for d in (report.get("opened_details") or [])
                        if isinstance(d, dict)
                    }
                    for key in opened:
                        detail = opened_map.get(str(key), {})
                        token_alive = detail.get("token_alive")
                        balance = detail.get("balance")
                        status = detail.get("status")
                        code = detail.get("code")
                        ready_to_bet = detail.get("ready_to_bet")
                        enter_ok = detail.get("enter_table_ok")
                        enter_method = detail.get("enter_table_method")
                        msg = f"[ALLGAME] + Đang Chơi: {_key_label(key)}"
                        if token_alive is not None:
                            msg += f" | token_alive={bool(token_alive)}"
                        if balance is not None:
                            msg += f" | balance={balance}"
                        if status is not None or code is not None:
                            msg += f" | api_status={status} code={code}"
                        if ready_to_bet is not None:
                            msg += f" | ready_to_bet={bool(ready_to_bet)}"
                        if enter_ok is not None:
                            msg += f" | enter_table={bool(enter_ok)}"
                        if enter_method:
                            msg += f" ({enter_method})"
                        print(msg, flush=True)
                if closed:
                    for key in closed:
                        print(f"[ALLGAME] - Rời Đang Chơi: {_key_label(key)}", flush=True)
                if skipped:
                    skipped_map = {
                        str(d.get("session_key")): d
                        for d in (report.get("skipped_details") or [])
                        if isinstance(d, dict)
                    }
                    for key in skipped:
                        detail = skipped_map.get(str(key), {})
                        reason = detail.get("error")
                        msg = f"[ALLGAME] ~ Bỏ qua mở session: {_key_label(key)}"
                        if reason:
                            msg += f" | reason={reason}"
                        print(msg, flush=True)
                        d = detail.get("detail")
                        if isinstance(d, dict):
                            short = (
                                f"enter={d.get('enter_table_ok')} "
                                f"ws={d.get('ws_connected')} "
                                f"ready={d.get('ready_to_bet')} "
                                f"recv={d.get('parsed_frames')} "
                                f"url={str(d.get('final_page_url') or '')[-48:]}"
                            )
                            print(f"[ALLGAME]   detail: {short}", flush=True)
            # Chỉ 1 account reporter WS: account ready_to_bet đầu tiên.
            reg = get_registry()
            ready = [
                s
                for s in reg.list_all()
                if str(s.state or "") == "ready_to_bet" and str(s.chrome_cdp_url or "").strip()
            ]
            ready.sort(key=lambda s: s.session_key)
            if ready:
                r0 = ready[0]
                ensure_reporter(
                    candidate_key=r0.session_key,
                    portal_id=str(r0.portal_id),
                    username=str(r0.username),
                    cdp_url=str(r0.chrome_cdp_url),
                )
            else:
                stop_reporter()
        except Exception as e:
            print(f"[ALLGAME] reconcile error: {e}", flush=True)
        _stop.wait(interval)
