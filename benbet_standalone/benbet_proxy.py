# -*- coding: utf-8 -*-
"""SOCKS5 proxy cho HTTP (requests) và WebSocket (taixiu game)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def parse_socks5(proxy_str: str) -> tuple[str, int, str, str]:
    """
    host:port hoặc host:port:user:pass (pass có thể chứa ':').
    Chấp nhận tiền tố socks5:// / socks5h://
    """
    s = (proxy_str or "").strip()
    if not s:
        raise ValueError("proxy rỗng")
    for prefix in ("socks5h://", "socks5://", "http://", "https://"):
        if s.lower().startswith(prefix):
            s = s[len(prefix) :]
    parts = s.split(":")
    if len(parts) >= 4:
        host = parts[0].strip()
        port = int(parts[1].strip())
        user = parts[2].strip()
        pwd = ":".join(parts[3:]).strip()
        return host, port, user, pwd
    if len(parts) == 2:
        return parts[0].strip(), int(parts[1].strip()), "", ""
    raise ValueError(
        'Proxy SOCKS5: "host:port" hoặc "host:port:user:pass", nhận: ' + repr(proxy_str)
    )


def require_socks_deps(for_websocket: bool = False) -> None:
    """
  HTTP (requests): cần PySocks.
  WebSocket (websocket-client >= 1.6): cần python-socks.
    """
    try:
        import socks  # noqa: F401  # PySocks
    except ImportError as exc:
        raise RuntimeError(
            "Thiếu PySocks cho HTTP qua SOCKS5. Chạy: pip install PySocks"
        ) from exc
    if for_websocket:
        try:
            from python_socks.sync import Proxy  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "Thiếu python-socks cho WebSocket qua SOCKS5. "
                "Chạy: pip install \"python-socks[sync]\""
            ) from exc


def proxy_label(proxy_str: str) -> str:
    try:
        host, port, user, _ = parse_socks5(proxy_str)
        return f"socks5 {host}:{port}" + (f" ({user})" if user else "")
    except Exception:
        s = (proxy_str or "").strip()
        return s[:50] + ("…" if len(s) > 50 else "")


@dataclass
class BenbetProxy:
    raw: str

    @classmethod
    def from_string(cls, proxy: str | None) -> BenbetProxy | None:
        if not proxy or not str(proxy).strip():
            return None
        return cls(raw=str(proxy).strip())

    def requests_proxies(self) -> dict[str, str]:
        host, port, user, pwd = parse_socks5(self.raw)
        if user:
            url = f"socks5h://{user}:{pwd}@{host}:{port}"
        else:
            url = f"socks5h://{host}:{port}"
        return {"http": url, "https": url}

    def websocket_run_forever_kwargs(self) -> dict[str, Any]:
        """Tham số proxy cho websocket.WebSocketApp.run_forever."""
        host, port, user, pwd = parse_socks5(self.raw)
        kw: dict[str, Any] = {
            "http_proxy_host": host,
            "http_proxy_port": port,
            # socks5h = resolve DNS qua proxy (giống socks5h:// của requests)
            "proxy_type": "socks5h",
        }
        if user:
            kw["http_proxy_auth"] = (user, pwd)
        return kw

    def mount_session(self, session: Any) -> None:
        session.proxies.update(self.requests_proxies())
