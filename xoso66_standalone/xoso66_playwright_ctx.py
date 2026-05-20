# -*- coding: utf-8 -*-
"""
Playwright + SOCKS5 — dùng chung cho register / CF / bind / deposit.

Trên Windows: sync Playwright cần SelectorEventLoop (subprocess).
Không bọc thêm thread khi đã gọi từ asyncio.to_thread / ThreadPoolExecutor
(tránh NotImplementedError lồng thread).
"""

from __future__ import annotations

import queue
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Generator

from xoso66_proxy import ensure_proxy, playwright_proxy, site_host

if sys.platform.startswith("win"):
    import asyncio

    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

_pw_pool: ThreadPoolExecutor | None = None
_pw_pool_lock = threading.Lock()


def _playwright_thread_setup() -> None:
    import asyncio

    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        old = asyncio.get_event_loop()
        if not old.is_closed():
            old.close()
    except RuntimeError:
        pass
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)


def _use_playwright_thread() -> bool:
    """
    Chỉ offload sang thread Playwright khi đang trong asyncio event loop.
    Đã ở worker thread (to_thread / executor) → chạy trực tiếp sau _playwright_thread_setup.
    """
    try:
        import asyncio

        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _get_pw_pool() -> ThreadPoolExecutor:
    global _pw_pool
    with _pw_pool_lock:
        if _pw_pool is None:
            _pw_pool = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="xoso66-playwright"
            )
    return _pw_pool


def run_playwright_thread(fn, *args, **kwargs):
    """Chạy hàm sync (Playwright) trên thread an toàn Windows."""

    def wrapped() -> Any:
        _playwright_thread_setup()
        return fn(*args, **kwargs)

    return _get_pw_pool().submit(wrapped).result(timeout=600)


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


def _playwright_browser_threaded(
    session: dict,
    *,
    base_url: str,
    headless: bool,
    extra_http_headers: dict[str, str] | None,
) -> Generator[tuple[Any, Any, Any], None, None]:
    q: queue.Queue = queue.Queue()
    holder: dict[str, Any] = {}

    def worker() -> None:
        _playwright_thread_setup()
        try:
            holder["cm"] = _playwright_browser_impl(
                session,
                base_url=base_url,
                headless=headless,
                extra_http_headers=extra_http_headers,
            )
            trip = holder["cm"].__enter__()
            q.put(("enter", trip))
            cmd = q.get()
            holder["cm"].__exit__(cmd[0], cmd[1], cmd[2])
        except Exception as e:
            q.put(("fail", e))

    t = threading.Thread(target=worker, daemon=True, name="xoso66-playwright-cm")
    t.start()
    msg = q.get(timeout=600)
    if msg[0] == "fail":
        t.join(timeout=30)
        raise msg[1]
    trip = msg[1]
    try:
        yield trip
    except BaseException as e:
        q.put((type(e), e, e.__traceback__))
        raise
    else:
        q.put((None, None, None))
    t.join(timeout=600)


@contextmanager
def playwright_browser(
    session: dict,
    *,
    base_url: str,
    headless: bool = True,
    extra_http_headers: dict[str, str] | None = None,
) -> Generator[tuple[Any, Any, Any], None, None]:
    """
    Yield (playwright, browser, context). Caller tạo page và đóng browser.
    """
    if _use_playwright_thread():
        yield from _playwright_browser_threaded(
            session,
            base_url=base_url,
            headless=headless,
            extra_http_headers=extra_http_headers,
        )
        return
    with _playwright_browser_impl(
        session,
        base_url=base_url,
        headless=headless,
        extra_http_headers=extra_http_headers,
    ) as trip:
        yield trip
