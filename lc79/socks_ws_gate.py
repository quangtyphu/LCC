"""Giới hạn số thao tác SOCKS WebSocket đồng thời (Windows selector)."""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

_sem: Optional[asyncio.Semaphore] = None


def _limit() -> int:
    return 1 if sys.platform.startswith("win") else 4


def _get_sem() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(_limit())
    return _sem


def reset_socks_ws_gate() -> None:
    """Gọi khi tạo event loop mới (sau RECOVER)."""
    global _sem
    _sem = None


@asynccontextmanager
async def socks_ws_slot() -> AsyncIterator[None]:
    sem = _get_sem()
    await sem.acquire()
    try:
        yield
    finally:
        sem.release()
