# -*- coding: utf-8 -*-
"""Selector fd trace trên WS worker loop — log khi đóng fd còn add_reader / 10038."""
from __future__ import annotations

import os
import socket
import traceback
from collections import deque
from typing import Any, Callable

try:
    import socks
except Exception:  # pragma: no cover
    socks = None  # type: ignore


def _brief_stack(skip: int = 2, limit: int = 14) -> str:
    frames = traceback.extract_stack(limit=limit + skip)[:-skip]
    keep = []
    for fr in frames:
        fn = fr.filename.replace("\\", "/")
        if "/site-packages/" in fn or "/Lib/asyncio/" in fn:
            continue
        if "/Lib/threading.py" in fn or "/Lib/selectors.py" in fn:
            continue
        keep.append(f"{os.path.basename(fr.filename)}:{fr.lineno}:{fr.name}")
    return " | ".join(keep[-8:]) or "?"


class WsSelectorTrace:
    def __init__(self) -> None:
        self.registered: dict[int, str] = {}
        self.closes: dict[int, str] = {}
        self.events: deque[str] = deque(maxlen=40)
        self._orig: dict[str, Any] = {}
        self.installed = False

    def install(self, loop: Any) -> "WsSelectorTrace":
        if self.installed:
            return self
        self.installed = True
        self._orig["add_reader"] = loop.add_reader
        self._orig["remove_reader"] = loop.remove_reader
        self._orig["socket_close"] = socket.socket.close
        if socks is not None:
            self._orig["socks_close"] = socks.socksocket.close
        self._orig["os_close"] = os.close
        sel = getattr(loop, "_selector", None)
        if sel is not None and hasattr(sel, "select"):
            self._orig["sel_select"] = sel.select
        trace = self

        def add_reader(fd, callback, *args):
            try:
                n = int(fd)
            except Exception:
                n = -1
            trace.registered[n] = _brief_stack()
            return trace._orig["add_reader"](fd, callback, *args)

        def remove_reader(fd):
            try:
                n = int(fd)
            except Exception:
                n = -1
            trace.registered.pop(n, None)
            return trace._orig["remove_reader"](fd)

        loop.add_reader = add_reader  # type: ignore[method-assign]
        loop.remove_reader = remove_reader  # type: ignore[method-assign]

        if "sel_select" in self._orig:
            def sel_select(timeout=None):
                try:
                    return trace._orig["sel_select"](timeout)
                except OSError as e:
                    if getattr(e, "winerror", None) == 10038 or "10038" in str(e):
                        print(trace.dump(e), flush=True)
                    raise

            sel.select = sel_select  # type: ignore[method-assign]

        def _close_with_fd(orig: Callable, sock_obj: socket.socket) -> None:
            fd = -1
            try:
                fd = int(sock_obj.fileno())
            except Exception:
                fd = -1
            if fd in trace.registered:
                st = _brief_stack(skip=3)
                trace.closes[fd] = st
                msg = f"[WS-DIAG] DIRTY_CLOSE fd={fd} still-in-selector {st}"
                trace.events.append(msg)
                print(msg, flush=True)
            orig(sock_obj)

        def socket_close(self_sock):
            return _close_with_fd(trace._orig["socket_close"], self_sock)

        socket.socket.close = socket_close  # type: ignore[method-assign]
        if socks is not None and "socks_close" in self._orig:
            def socks_close(self_sock):
                return _close_with_fd(trace._orig["socks_close"], self_sock)

            socks.socksocket.close = socks_close  # type: ignore[method-assign]

        def os_close(fd):
            try:
                n = int(fd)
            except Exception:
                n = -1
            if n in trace.registered:
                st = _brief_stack(skip=3)
                trace.closes[n] = st
                msg = f"[WS-DIAG] DIRTY_OS_CLOSE fd={n} still-in-selector {st}"
                trace.events.append(msg)
                print(msg, flush=True)
            return trace._orig["os_close"](fd)

        os.close = os_close  # type: ignore[assignment]
        return self

    def restore(self) -> None:
        if not self.installed:
            return
        if "socket_close" in self._orig:
            socket.socket.close = self._orig["socket_close"]  # type: ignore[method-assign]
        if socks is not None and "socks_close" in self._orig:
            socks.socksocket.close = self._orig["socks_close"]  # type: ignore[method-assign]
        if "os_close" in self._orig:
            os.close = self._orig["os_close"]  # type: ignore[assignment]
        self.installed = False

    def dump(self, exc: BaseException | None = None) -> str:
        lines = ["[WS-DIAG] --- selector dump ---"]
        if exc is not None:
            lines.append(f"[WS-DIAG] exc={type(exc).__name__}: {exc}")
        dirty = [fd for fd in self.registered if fd in self.closes]
        lines.append(f"[WS-DIAG] registered_n={len(self.registered)} dirty={dirty}")
        for fd in dirty:
            lines.append(f"[WS-DIAG]   fd={fd} add={self.registered.get(fd)}")
            lines.append(f"[WS-DIAG]   fd={fd} close={self.closes.get(fd)}")
        for e in list(self.events)[-12:]:
            lines.append(e if e.startswith("[WS-DIAG]") else f"[WS-DIAG] {e}")
        return "\n".join(lines)
