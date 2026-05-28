# -*- coding: utf-8 -*-
"""WebSocket h54uk trực tiếp từ Python (không CDP) — sau khi tắt Chrome."""
from __future__ import annotations

import json
import ssl
import threading
import time
from typing import Any, Callable
from urllib.parse import urlparse

try:
    import websocket as ws_lib
except ImportError:
    ws_lib = None  # type: ignore

from c168_mex_protocol import decode_mex_frame
from c168_vendor_ws_sniff import decode_frame

FrameCallback = Callable[[str, str, str], None]

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


def origin_from_ws_url(url: str) -> str:
    p = urlparse(url or "")
    if p.scheme and p.netloc:
        return f"{p.scheme}://{p.netloc}"
    return "https://bpcdf.mhuxu.com"


def _raw_to_text(message: str | bytes) -> str:
    if isinstance(message, bytes):
        text, _ = decode_mex_frame(message)
        return text if isinstance(text, str) else message.decode("utf-8", errors="replace")
    return decode_frame(str(message))


class H54ukWsClient:
    def __init__(
        self,
        url: str,
        on_frame: FrameCallback,
        *,
        proxy_server: str = "",
        headers: list[str] | None = None,
    ) -> None:
        self.url = url
        self.on_frame = on_frame
        self.proxy_server = (proxy_server or "").strip()
        self.headers = list(headers or [])
        self._ws: Any = None
        self._thread: threading.Thread | None = None
        self._hb_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.frame_count = 0
        self._last_error = ""
        self._connected = False
        self._initialized = False
        self._server_ts = 0

    def _send_json(self, ws: Any, obj: dict[str, Any]) -> None:
        ws.send(json.dumps(obj, separators=(",", ":")))

    def _on_message(self, _ws: Any, message: Any) -> None:
        if self._stop.is_set():
            return
        text = _raw_to_text(message)
        self.frame_count += 1
        try:
            self.on_frame("recv", self.url, text)
        except Exception:
            pass
        _, obj = decode_mex_frame(text if isinstance(text, str) else text)
        if not isinstance(obj, dict) or self._initialized:
            return
        mt = str(obj.get("messageType") or "")
        if mt in ("Initialize", "Connecting") and not self._initialized:
            try:
                self._send_json(_ws, {"router": "userPlainBalance", "ts": 0})
                self._send_json(_ws, {"router": "serverInfo", "ts": 0})
                self._initialized = True
            except Exception:
                pass

    def _on_open(self, ws: Any) -> None:
        self._connected = True
        self._ws = ws
        try:
            self._send_json(ws, {"router": "initialization"})
        except Exception:
            pass

    def _on_error(self, _ws: Any, err: Any) -> None:
        msg = str(err)
        if "Invalid close opcode" in msg and (self.frame_count > 0 or self._connected):
            return
        self._last_error = msg

    def _on_close(self, _ws: Any, *args: Any) -> None:
        self._connected = False

    def _maintain_loop(self) -> None:
        while not self._stop.wait(3.0):
            ws = self._ws
            if not ws:
                continue
            try:
                self._send_json(ws, {"router": "heartbeat"})
                if self._server_ts:
                    self._send_json(ws, {"router": "serverInfo", "ts": self._server_ts})
                    self._send_json(
                        ws, {"router": "userPlainBalance", "ts": self._server_ts}
                    )
                self._server_ts = int(time.time() * 1000)
            except Exception:
                pass

    def start(self, *, retries: int = 2) -> bool:
        if ws_lib is None:
            self._last_error = "pip install websocket-client"
            return False
        from c168_proxy import websocket_run_forever_kwargs

        hdr = list(self.headers)
        if not any(h.lower().startswith("user-agent:") for h in hdr):
            hdr.append(f"User-Agent: {_DEFAULT_UA}")

        def run() -> None:
            self._ws = ws_lib.WebSocketApp(
                self.url,
                header=hdr,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            kwargs: dict[str, Any] = {
                "ping_interval": 20,
                "ping_timeout": 10,
                "sslopt": {"cert_reqs": ssl.CERT_NONE},
            }
            kwargs.update(websocket_run_forever_kwargs(self.proxy_server))
            self._ws.run_forever(**kwargs)

        for attempt in range(max(1, retries)):
            self._stop.clear()
            self._last_error = ""
            self.frame_count = 0
            self._connected = False
            self._initialized = False
            try:
                if self._ws:
                    self._ws.close()
            except Exception:
                pass
            self._thread = threading.Thread(target=run, daemon=True)
            self._thread.start()
            self._hb_thread = threading.Thread(target=self._maintain_loop, daemon=True)
            self._hb_thread.start()
            for _ in range(40):
                time.sleep(0.5)
                if self.frame_count > 2:
                    return True
                err = self._last_error or ""
                if err and "Invalid close opcode" not in err:
                    break
            if self.frame_count > 0:
                return True
            try:
                if self._ws:
                    self._ws.close()
            except Exception:
                pass
            if attempt + 1 < retries:
                time.sleep(1.0)
        if "Invalid close opcode" in self._last_error:
            self._last_error = (
                "h54uk đóng ngay (token hết hạn) — chạy lại --headless-play"
            )
        return False

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
