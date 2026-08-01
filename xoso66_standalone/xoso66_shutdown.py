# -*- coding: utf-8 -*-
"""Tín hiệu dừng chung cho main.py và các worker (Ctrl+C / SIGTERM)."""

from __future__ import annotations

import threading
import time
from typing import Any

_stop = threading.Event()
_api_server: Any = None
_deposit_handler: Any = None
_lock = threading.Lock()


def request_stop() -> None:
    _stop.set()
    try:
        from xoso66_minigame_ws_worker import cancel_ws_pool_pending_work

        cancel_ws_pool_pending_work()
    except Exception:
        pass
    try:
        from xoso66_playwright_ctx import shutdown_playwright_pool

        shutdown_playwright_pool(wait=False)
    except Exception:
        pass
    with _lock:
        api_srv = _api_server
        dep_srv = _deposit_handler
    if api_srv is not None:
        try:
            api_srv.should_exit = True
        except Exception:
            pass
    if dep_srv is not None:
        try:
            dep_srv.shutdown()
        except Exception:
            pass


def stopping() -> bool:
    return _stop.is_set()


def sleep_interruptible(seconds: float) -> bool:
    """Ngủ tối đa `seconds` giây; False nếu request_stop() (Ctrl+C)."""
    if stopping():
        return False
    end = time.time() + max(0.0, float(seconds))
    while time.time() < end:
        if stopping():
            return False
        time.sleep(min(1.0, end - time.time()))
    return not stopping()


def register_api_server(server: Any) -> None:
    global _api_server
    with _lock:
        _api_server = server


def clear_api_server() -> None:
    global _api_server
    with _lock:
        _api_server = None


def register_deposit_handler(server: Any) -> None:
    global _deposit_handler
    with _lock:
        _deposit_handler = server


def clear_deposit_handler() -> None:
    global _deposit_handler
    with _lock:
        _deposit_handler = None
