"""Bridge coroutines from background threads onto the main watcher event loop."""

from __future__ import annotations

import asyncio
from typing import Coroutine, Optional, TypeVar

T = TypeVar("T")

_watcher_loop: Optional[asyncio.AbstractEventLoop] = None


def register_watcher_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _watcher_loop
    _watcher_loop = loop


def unregister_watcher_loop() -> None:
    global _watcher_loop
    _watcher_loop = None


def get_watcher_loop() -> Optional[asyncio.AbstractEventLoop]:
    return _watcher_loop


def _log_future_exception(fut: asyncio.Future) -> None:
    if fut.cancelled():
        return
    exc = fut.exception()
    if exc is not None:
        print(f"⚠️ schedule_fire_and_forget failed: {exc}", flush=True)


def run_on_watcher_loop(coro: Coroutine[None, None, T], timeout: float | None = 60) -> T:
    """
    Run a coroutine on the main watcher loop and block until done.

    Flask/scheduler threads must use this instead of ``asyncio.run`` while
    ``watcher_loop`` is active — nested loops on Windows corrupt the selector
    (WinError 10038) when SOCKS websockets are in use.
    """
    watcher = _watcher_loop
    if watcher is not None and watcher.is_running():
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is watcher:
            raise RuntimeError("run_on_watcher_loop cannot block inside watcher_loop")
        fut = asyncio.run_coroutine_threadsafe(coro, watcher)
        return fut.result(timeout=timeout)
    return asyncio.run(coro)


def schedule_fire_and_forget(coro: Coroutine[None, None, T]) -> None:
    """
    Schedule a coroutine without blocking the caller.

    - Running inside the watcher loop -> create_task (same as before).
    - Other thread while watcher is running -> run_coroutine_threadsafe.
    - No watcher (CLI standalone) -> asyncio.run fallback.
    """
    watcher = _watcher_loop
    if watcher is not None and watcher.is_running():
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is watcher:
            watcher.create_task(coro)
            return
        fut = asyncio.run_coroutine_threadsafe(coro, watcher)
        fut.add_done_callback(_log_future_exception)
        return
    asyncio.run(coro)
