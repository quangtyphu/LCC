# -*- coding: utf-8 -*-
"""
Playwright + SOCKS5 — dùng chung cho register / CF / bind / deposit.

Quy tắc:
  - Sync Playwright (greenlet) chỉ gọi trên MỘT thread — không yield browser/context sang thread khác.
  - Windows + main.py (Selector policy): _playwright_thread_setup() đặt Proactor trước new_event_loop().
  - Trong asyncio loop đang chạy: dùng run_playwright_browser(..., fn=...) (pool 1 worker).
"""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Callable, Generator, TypeVar

from xoso66_proxy import ensure_proxy, playwright_proxy, site_host

T = TypeVar("T")

_PW_THREAD_PREFIX = "xoso66-playwright"

_pw_pool: ThreadPoolExecutor | None = None
_pw_pool_lock = threading.Lock()


def _on_playwright_worker_thread() -> bool:
    return threading.current_thread().name.startswith(_PW_THREAD_PREFIX)


def _in_running_asyncio() -> bool:
    try:
        import asyncio

        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _playwright_thread_setup() -> None:
    import asyncio

    if sys.platform.startswith("win"):
        # Playwright sync API: self._loop = asyncio.new_event_loop() — theo policy hiện tại.
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        old = asyncio.get_event_loop()
        if not old.is_closed():
            old.close()
    except RuntimeError:
        pass
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)


def _pw_pool_initializer() -> None:
    _playwright_thread_setup()


def _get_pw_pool() -> ThreadPoolExecutor:
    global _pw_pool
    with _pw_pool_lock:
        if _pw_pool is None:
            _pw_pool = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=_PW_THREAD_PREFIX,
                initializer=_pw_pool_initializer,
            )
    return _pw_pool


def _run_in_playwright_pool(fn: Callable[[], T]) -> T:
    if _on_playwright_worker_thread():
        return fn()
    return _get_pw_pool().submit(fn).result(timeout=600)


def run_playwright_thread(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Chạy hàm sync (Playwright) trên thread pool (khi cần)."""

    def wrapped() -> T:
        _playwright_thread_setup()
        return fn(*args, **kwargs)

    if _on_playwright_worker_thread():
        return wrapped()
    if _in_running_asyncio():
        return _run_in_playwright_pool(wrapped)
    return wrapped()


def run_playwright_browser(
    session: dict,
    fn: Callable[[Any, Any, Any], T],
    *,
    base_url: str,
    headless: bool = True,
    extra_http_headers: dict[str, str] | None = None,
) -> T:
    """
    Chạy fn(playwright, browser, context) trên đúng thread Playwright.
    Dùng khi caller nằm trong asyncio loop; hoặc thay cho with playwright_browser.
    """

    def _run() -> T:
        with _playwright_browser_impl(
            session,
            base_url=base_url,
            headless=headless,
            extra_http_headers=extra_http_headers,
        ) as trip:
            return fn(*trip)

    if _in_running_asyncio():
        return _run_in_playwright_pool(_run)
    return _run()


@contextmanager
def _playwright_browser_impl(
    session: dict,
    *,
    base_url: str,
    headless: bool = True,
    extra_http_headers: dict[str, str] | None = None,
) -> Generator[tuple[Any, Any, Any], None, None]:
    _playwright_thread_setup()
    from playwright.sync_api import sync_playwright

    proxy_str = ensure_proxy(session)
    px = playwright_proxy(proxy_str)
    host = site_host(base_url)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, proxy=px)
        ctx_kw: dict[str, Any] = {}
        if extra_http_headers:
            ctx_kw["extra_http_headers"] = extra_http_headers
        if session.get("user_agent"):
            ctx_kw["user_agent"] = session["user_agent"]
        context = browser.new_context(**ctx_kw)

        existing = session.get("cookies") or {}
        if existing:
            pw_cookies = []
            for name, value in existing.items():
                if value is None:
                    continue
                pw_cookies.append(
                    {
                        "name": str(name),
                        "value": str(value),
                        "domain": host,
                        "path": "/",
                    }
                )
            if pw_cookies:
                try:
                    context.add_cookies(pw_cookies)
                except Exception:
                    pass

        try:
            yield p, browser, context
        finally:
            browser.close()


@contextmanager
def playwright_browser(
    session: dict,
    *,
    base_url: str,
    headless: bool = True,
    extra_http_headers: dict[str, str] | None = None,
) -> Generator[tuple[Any, Any, Any], None, None]:
    """
    Yield (playwright, browser, context) trên thread hiện tại.

    Không dùng bên trong coroutine asyncio đang chạy — dùng run_playwright_browser().
    Mọi thao tác page/context phải nằm trong khối with (cùng thread).
    """
    if _in_running_asyncio():
        raise RuntimeError(
            "playwright_browser không dùng trong asyncio loop; "
            "dùng run_playwright_browser(session, fn, base_url=...)."
        )
    with _playwright_browser_impl(
        session,
        base_url=base_url,
        headless=headless,
        extra_http_headers=extra_http_headers,
    ) as trip:
        yield trip
