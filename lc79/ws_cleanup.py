"""Đóng WebSocket + SOCKS socket an toàn trên Windows (giảm WinError 10038)."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any


async def close_ws_socks_clean(
    ws: Any,
    sock: Any,
    namespace: str = "tx",
    *,
    sock_handoff: bool = False,
) -> None:
    """
    sock_handoff=True khi sock đã truyền vào websockets.connect(sock=...) —
    websockets sẽ đóng socket khi ws.close(); không gọi sock.close() thêm lần nữa.
    """
    if ws is not None:
        with contextlib.suppress(Exception):
            if ws.open:
                await ws.send(f"41/{namespace}")
                await asyncio.sleep(0.1)
                with contextlib.suppress(OSError):
                    await asyncio.wait_for(ws.close(), timeout=2.0)
                wait_closed = getattr(ws, "wait_closed", None)
                if callable(wait_closed):
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(wait_closed(), timeout=3.0)
    if sock is not None and not sock_handoff:
        with contextlib.suppress(OSError, Exception):
            sock.close()
