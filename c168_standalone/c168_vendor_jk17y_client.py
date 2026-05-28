# -*- coding: utf-8 -*-
"""WS lobby jk17y (tel617) — lobbyTableClick khi không còn Chrome."""
from __future__ import annotations

import json
import ssl
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    import websocket as ws_lib
except ImportError:
    ws_lib = None  # type: ignore

from c168_vendor_virtual_table import (
    _b64_json,
    build_lobby_view_payload,
    build_table_click_payload,
    decode_user_id_from_h54uk_token,
)

DEFAULT_JK17Y_URL = "wss://tel617.delta9968.com/jk17y/"

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


def user_id_from_h54uk_url(h54uk_url: str) -> str:
    qs = parse_qs(urlparse(h54uk_url or "").query)
    token = (qs.get("token") or [""])[0]
    return decode_user_id_from_h54uk_token(token)


def _router_msg(router: str, data_key: str, inner: dict[str, Any]) -> str:
    return json.dumps(
        {"router": router, "data": {data_key: _b64_json(inner)}},
        separators=(",", ":"),
    )


class Jk17yLobbyClient:
    """Giữ WS jk17y + heartbeat — server mới push GameInfo/GP_NEW_GAME_START trên h54uk."""

    def __init__(
        self,
        *,
        user_id: str,
        table_id: int = 1006,
        url: str = DEFAULT_JK17Y_URL,
        origin: str = "https://bpcdf.mhuxu.com",
        proxy_server: str = "",
        cookies: dict[str, str] | None = None,
    ) -> None:
        self.user_id = (user_id or "").strip()
        self.table_id = int(table_id)
        self.url = (url or DEFAULT_JK17Y_URL).strip()
        self.origin = (origin or "https://bpcdf.mhuxu.com").rstrip("/")
        self.proxy_server = (proxy_server or "").strip()
        self.cookies = dict(cookies or {})
        self._ws: Any = None
        self._thread: threading.Thread | None = None
        self._hb_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.entered = False
        self._lobby_sent = False
        self._last_error = ""

    def _headers(self) -> list[str]:
        hdr = [
            f"Origin: {self.origin}",
            f"User-Agent: {_DEFAULT_UA}",
        ]
        if self.cookies:
            hdr.append(
                "Cookie: " + "; ".join(f"{k}={v}" for k, v in self.cookies.items())
            )
        return hdr

    def _send_enter(self, ws: Any) -> None:
        if not self.user_id:
            return
        loading = {
            "category": "Lobby",
            "label": "LoadingView",
            "userID": self.user_id,
            "website": "327",
            "currency": "11",
            "accountCreateDate": "1779615398",
            "device": 4,
        }
        view = build_lobby_view_payload(user_id=self.user_id)
        click = build_table_click_payload(self.table_id, user_id=self.user_id)
        ws.send(_router_msg("lobbyLoadingView", "lobbyLoadingView", loading))
        time.sleep(0.35)
        ws.send(_router_msg("lobbyLobbyView", "lobbyLobbyView", view))
        time.sleep(0.15)
        ws.send(_router_msg("lobbyTableClick", "lobbyTableClick", click))
        time.sleep(0.2)
        engage = {
            "category": "Lobby",
            "label": "Engagement",
            "userID": self.user_id,
            "website": "327",
            "currency": "11",
            "accountCreateDate": "1779615398",
            "duration": "42",
            "device": 4,
        }
        ws.send(_router_msg("lobbyEngagement", "lobbyEngagement", engage))
        self.entered = True
        self._lobby_sent = True

    def send_table_click_again(self) -> bool:
        ws = self._ws
        if not ws or not self.user_id:
            return False
        try:
            click = build_table_click_payload(self.table_id, user_id=self.user_id)
            ws.send(_router_msg("lobbyTableClick", "lobbyTableClick", click))
            return True
        except Exception:
            return False

    def _on_open(self, ws: Any) -> None:
        self._ws = ws

    def _on_message(self, _ws: Any, message: Any) -> None:
        if self._lobby_sent or self._stop.is_set():
            return
        text = message if isinstance(message, str) else str(message)
        if "connecting" in text.lower() or '"status":"200"' in text:
            try:
                self._send_enter(_ws)
            except Exception as exc:
                self._last_error = str(exc)

    def _on_error(self, _ws: Any, err: Any) -> None:
        self._last_error = str(err)

    def _on_close(self, _ws: Any, *args: Any) -> None:
        self._ws = None

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(12.0):
            ws = self._ws
            if not ws:
                continue
            try:
                ws.send(json.dumps({"router": "heartbeat"}))
            except Exception:
                pass

    def start(self) -> bool:
        if ws_lib is None:
            self._last_error = "pip install websocket-client"
            return False
        if not self.user_id:
            self._last_error = "no_user_id"
            return False
        from c168_proxy import websocket_run_forever_kwargs

        def run() -> None:
            app = ws_lib.WebSocketApp(
                self.url,
                header=self._headers(),
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            self._ws = app
            kw: dict[str, Any] = {
                "ping_interval": 20,
                "ping_timeout": 10,
                "sslopt": {"cert_reqs": ssl.CERT_NONE},
            }
            kw.update(websocket_run_forever_kwargs(self.proxy_server))
            app.run_forever(**kw)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        self._hb_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._hb_thread.start()
        for _ in range(20):
            time.sleep(0.3)
            if self.entered:
                return True
            if self._last_error:
                return False
        if self._ws and not self._lobby_sent:
            try:
                self._send_enter(self._ws)
            except Exception as exc:
                self._last_error = str(exc)
        return self.entered

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass


def headless_register_table(
    *,
    user_id: str,
    table_id: int = 1006,
    h54uk_url: str = "",
    origin: str = "",
    proxy_server: str = "",
    jk17y_url: str = DEFAULT_JK17Y_URL,
    cookies: dict[str, str] | None = None,
) -> tuple[Jk17yLobbyClient | None, dict[str, Any]]:
    uid = (user_id or "").strip() or user_id_from_h54uk_url(h54uk_url)
    if not uid:
        return None, {"ok": False, "error": "no_user_id"}
    orig = (origin or "").strip()
    if not orig and h54uk_url:
        p = urlparse(h54uk_url)
        if p.scheme and p.netloc:
            orig = f"{p.scheme}://{p.netloc}"
    client = Jk17yLobbyClient(
        user_id=uid,
        table_id=table_id,
        url=jk17y_url,
        origin=orig or "https://bpcdf.mhuxu.com",
        proxy_server=proxy_server,
        cookies=cookies,
    )
    ok = client.start()
    return client, {
        "ok": ok,
        "userID": uid,
        "error": client._last_error if not ok else "",
    }
