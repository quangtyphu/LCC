# -*- coding: utf-8 -*-
"""
Proxy SOCKS5 cho mọi traffic XOSO66 (giống LC79).

Format: host:port:user:pass  (vd. 118.70.171.104:20023:PogCLP:wSMZkU)

Cấu hình mặc định (theo thứ tự):
  1. session["proxy"]
  2. env XOSO66_DEFAULT_PROXY
  3. xoso66_config_util.DEFAULT_PROXY (hardcode)
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse


class ProxyRequiredError(ValueError):
    """Thiếu proxy — mọi API game bắt buộc đi qua SOCKS5."""


def parse_proxy(proxy_str: str) -> tuple[str, int, str, str]:
    """host:port:user:pass hoặc host:port."""
    s = (proxy_str or "").strip()
    if not s:
        raise ValueError("proxy rỗng")
    parts = s.split(":")
    if len(parts) == 4:
        host, port_s, user, pwd = parts
        return host.strip(), int(port_s), user.strip(), pwd.strip()
    if len(parts) == 2:
        return parts[0].strip(), int(parts[1]), "", ""
    raise ValueError(
        'proxy phải dạng "host:port:user:pass" hoặc "host:port", '
        f"nhận: {proxy_str!r}"
    )


def load_default_proxy() -> str:
    from xoso66_config_util import hardcoded_default_proxy

    return hardcoded_default_proxy()


def resolve_proxy(session: dict | None) -> str:
    """Proxy cho request: session trước, rồi default."""
    if session:
        p = (session.get("proxy") or "").strip()
        if p:
            return p
    return load_default_proxy()


def require_explicit_proxy(proxy_str: str | None) -> str:
    """
    Proxy bắt buộc do caller truyền (đăng ký / CMS provision).
    Không fallback default_proxy từ env/config.
    """
    p = (proxy_str or "").strip()
    if not p:
        raise ProxyRequiredError(
            "Thiếu proxy bắt buộc. Truyền proxy dạng host:port:user:pass "
            '(vd. "118.70.171.104:20023:user:pass"). '
            "Đăng ký không dùng default_proxy trong config."
        )
    parse_proxy(p)
    return p


def ensure_proxy(session: dict, *, explicit_only: bool = False) -> str:
    """
    Gắn proxy vào session.
    explicit_only=True: chỉ session['proxy'], không đọc default (đăng ký).
    """
    if explicit_only:
        p = require_explicit_proxy(session.get("proxy"))
    else:
        p = resolve_proxy(session)
        if not p:
            raise ProxyRequiredError(
                "Thiếu proxy. Thêm session['proxy'] hoặc "
                "XOSO66_DEFAULT_PROXY hoặc DEFAULT_PROXY trong xoso66_config_util.py "
                '(vd. "118.70.171.104:20023:user:pass").'
            )
    session["proxy"] = p
    return p


def build_proxies(proxy_str: str) -> dict[str, str]:
    """Dict cho requests / curl_cffi — socks5h như LC79."""
    host, port, user, pwd = parse_proxy(proxy_str)
    if user:
        auth = f"{user}:{pwd}@"
    else:
        auth = ""
    url = f"socks5h://{auth}{host}:{port}"
    return {"http": url, "https": url}


def proxy_has_auth(proxy_str: str) -> bool:
    """True nếu proxy dạng host:port:user:pass."""
    try:
        _, _, user, _ = parse_proxy(proxy_str)
        return bool(user)
    except ValueError:
        return False


def playwright_proxy(proxy_str: str) -> dict[str, str]:
    """Dict proxy cho Playwright (relay HTTP local nếu SOCKS5 có user/pass)."""
    from xoso66_proxy_relay import playwright_proxy_with_relay

    return playwright_proxy_with_relay(proxy_str)


def apply_requests_proxy(req_session: Any, proxy_str: str) -> None:
    req_session.proxies.update(build_proxies(proxy_str))


def proxy_log_label(proxy_str: str) -> str:
    """host:port cho log (không in password)."""
    try:
        host, port, user, _ = parse_proxy(proxy_str)
        if user:
            return f"{host}:{port} (socks5, user {user})"
        return f"{host}:{port} (socks5)"
    except Exception:
        s = (proxy_str or "").strip()
        return s[:48] + ("…" if len(s) > 48 else "") if s else "(không có proxy)"


def proxy_source_label(session: dict | None, *, account_proxy: str = "") -> str:
    """
    Mô tả proxy đang dùng: riêng acc hay default config.
    Gọi sau ensure_proxy(session).
    """
    used = str((session or {}).get("proxy") or "").strip()
    acc = str(account_proxy or "").strip()
    default = load_default_proxy()
    if acc:
        return f"proxy acc → {proxy_log_label(used)}"
    if used and default and used == default:
        return f"proxy default config → {proxy_log_label(used)}"
    if used:
        return f"proxy session → {proxy_log_label(used)}"
    return "proxy: (chưa gán)"


def site_host(base_url: str) -> str:
    return urlparse(base_url.rstrip("/")).netloc or "localhost"
