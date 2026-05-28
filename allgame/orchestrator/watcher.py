# -*- coding: utf-8 -*-
"""Thread watcher — gọi reconcile định kỳ."""

from __future__ import annotations

import threading
import time
from typing import Callable

from allgame.config_util import load_config
from allgame.orchestrator.reconcile import reconcile_once

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
            else:
                print(
                    f"[ALLGAME] reconcile tick: target={report.get('target_count')} "
                    f"active={report.get('active_count')} "
                    f"opened={len(opened)} closed={len(closed)} skipped={len(skipped)}",
                    flush=True,
                )
                if opened:
                    for key in opened:
                        print(f"[ALLGAME] + Đang Chơi: {_key_label(key)}", flush=True)
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
                        if detail.get("detail") is not None:
                            print(
                                f"[ALLGAME]   detail: {detail.get('detail')}",
                                flush=True,
                            )
                if not opened and not closed and not skipped:
                    print("[ALLGAME] không có thay đổi trạng thái", flush=True)
        except Exception as e:
            print(f"[ALLGAME] reconcile error: {e}", flush=True)
        _stop.wait(interval)
