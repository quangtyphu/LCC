# -*- coding: utf-8 -*-
"""
Playwright + SOCKS5 — dùng chung cho register / CF / bind / deposit.

Quy tắc:
  - Sync Playwright (greenlet) chỉ gọi trên MỘT thread — không yield browser/context sang thread khác.
  - Windows + main.py (Selector policy): _playwright_thread_setup() đặt Proactor trước new_event_loop().
  - Trong asyncio loop đang chạy: dùng run_playwright_browser(..., fn=...) (pool 1 worker).
"""

from __future__ import annotations

import hashlib
import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Generator, TypeVar

from xoso66_proxy import ensure_proxy, playwright_proxy, site_host

_DIR = Path(__file__).resolve().parent
_REGISTER_PROFILES = _DIR / "register_profiles"

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


def shutdown_playwright_pool(*, wait: bool = False) -> None:
    """Ctrl+C — thread pool Playwright (non-daemon) giữ process nếu không shutdown."""
    global _pw_pool
    with _pw_pool_lock:
        pool = _pw_pool
        _pw_pool = None
    if pool is not None:
        pool.shutdown(wait=wait, cancel_futures=True)


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
    channel: str | None = None,
    ignore_automation: bool = False,
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
            channel=channel,
            ignore_automation=ignore_automation,
        ) as trip:
            return fn(*trip)

    if _in_running_asyncio():
        return _run_in_playwright_pool(_run)
    return _run()


def _launch_chromium(p: Any, launch_kw: dict[str, Any]) -> Any:
    """Ưu tiên Chrome cài sẵn (ít bị CF hơn bundled Chromium)."""
    channel = launch_kw.pop("channel", None)
    if channel:
        try:
            return p.chromium.launch(channel=channel, **launch_kw)
        except Exception:
            pass
    return p.chromium.launch(**launch_kw)


@contextmanager
def _playwright_browser_impl(
    session: dict,
    *,
    base_url: str,
    headless: bool = True,
    extra_http_headers: dict[str, str] | None = None,
    channel: str | None = None,
    ignore_automation: bool = False,
) -> Generator[tuple[Any, Any, Any], None, None]:
    _playwright_thread_setup()
    from playwright.sync_api import sync_playwright

    proxy_str = ensure_proxy(session)
    px = playwright_proxy(proxy_str)
    host = site_host(base_url)

    launch_kw: dict[str, Any] = {"headless": headless}
    if px:
        launch_kw["proxy"] = px
    if channel:
        launch_kw["channel"] = channel
    if ignore_automation:
        launch_kw["ignore_default_args"] = ["--enable-automation"]

    with sync_playwright() as p:
        browser = _launch_chromium(p, launch_kw)
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
    channel: str | None = None,
    ignore_automation: bool = False,
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
        channel=channel,
        ignore_automation=ignore_automation,
    ) as trip:
        yield trip


def _register_profile_dir(proxy_str: str) -> Path:
    """Mỗi proxy = 1 profile Chrome cố định (giữ cf_clearance như mở tay)."""
    device = (os.environ.get("XOSO66_CMS_DEVICE") or "").strip()
    if device:
        try:
            from xoso66_cms_chrome import resolve_cms_chrome_by_device

            row = resolve_cms_chrome_by_device(device)
            if row and row.get("profile_dir"):
                p = Path(str(row["profile_dir"]))
                if p.is_dir():
                    print(f"[REGISTER] Dùng CMS profile {device}: {p}", flush=True)
                    return p
        except Exception as e:
            print(f"[REGISTER] CMS device {device}: {e}", flush=True)
    custom = (os.environ.get("XOSO66_REGISTER_PROFILE_DIR") or "").strip()
    if custom:
        return Path(custom)
    key = hashlib.sha256(proxy_str.strip().encode("utf-8")).hexdigest()[:20]
    return _REGISTER_PROFILES / key


_STEALTH_INIT_SCRIPT = (
    "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
)


def _free_tcp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _wait_cdp_port(port: int, *, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _browser_isolate_launch_chrome(
    *,
    profile_path: Path,
    proxy: str,
    cdp_port: int,
    start_url: str = "",
) -> Any:
    """chrome.exe native + CDP — giống mở Chrome tay qua browser_isolate."""
    from xoso66_session import BASE_URL

    root = _DIR.parent / "browser_isolate"
    if not root.is_dir():
        raise RuntimeError("Thiếu thư mục browser_isolate")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from launcher import _launch_chrome_native  # type: ignore

    url = (start_url or os.environ.get("XOSO66_REGISTER_START_URL") or f"{BASE_URL}/home/").strip()
    return _launch_chrome_native(
        profile_path=profile_path,
        proxy=proxy,
        url=url,
        cdp_port=cdp_port,
    )


@contextmanager
def _native_chrome_register_context(
    session: dict,
) -> Generator[tuple[Any, Any, Any], None, None]:
    proxy_str = ensure_proxy(session, explicit_only=True)
    profile_dir = _register_profile_dir(proxy_str)
    profile_dir.mkdir(parents=True, exist_ok=True)
    cdp_port = _free_tcp_port()
    proc = _browser_isolate_launch_chrome(
        profile_path=profile_dir,
        proxy=proxy_str,
        cdp_port=cdp_port,
    )
    if not _wait_cdp_port(cdp_port):
        try:
            proc.terminate()
        except Exception:
            pass
        raise RuntimeError(f"Chrome native không mở CDP port {cdp_port}")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        time.sleep(1.5)
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        try:
            yield p, browser, context
        finally:
            try:
                browser.close()
            except Exception:
                pass
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass


@contextmanager
def playwright_register_browser(
    session: dict,
    *,
    headless: bool = False,
    channel: str | None = "chrome",
) -> Generator[tuple[Any, Any], None, None]:
    """
    Chrome đăng ký — ưu tiên chrome.exe native (CDP), giống mở tay.

    Playwright launch_persistent vẫn bị CF đánh bot; chrome.exe + profile cố định
    theo proxy thì CF tin như trình duyệt thường.
    """
    if _in_running_asyncio():
        raise RuntimeError(
            "playwright_register_browser không dùng trong asyncio loop; "
            "dùng run_playwright_thread(register_account_playwright, ...)."
        )
    use_native = os.environ.get("XOSO66_REGISTER_NATIVE_CHROME", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    if use_native:
        _playwright_thread_setup()
        with _native_chrome_register_context(session) as (p, _browser, context):
            yield p, context
        return

    _playwright_thread_setup()
    from playwright.sync_api import sync_playwright

    proxy_str = ensure_proxy(session, explicit_only=True)
    profile_dir = _register_profile_dir(proxy_str)
    profile_dir.mkdir(parents=True, exist_ok=True)
    px = playwright_proxy(proxy_str)

    launch_kw: dict[str, Any] = {
        "user_data_dir": str(profile_dir),
        "headless": headless,
        "locale": "vi-VN",
        "timezone_id": "Asia/Ho_Chi_Minh",
        "viewport": {"width": 1366, "height": 768},
        "args": [
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
        ],
        "ignore_default_args": ["--enable-automation"],
    }
    if px:
        launch_kw["proxy"] = px
    if session.get("user_agent"):
        launch_kw["user_agent"] = session["user_agent"]

    with sync_playwright() as p:
        context = None
        channels = [channel] if channel else []
        channels.extend(ch for ch in ("chrome", "msedge", None) if ch not in channels)
        last_err: Exception | None = None
        for ch in channels:
            try:
                kw = dict(launch_kw)
                if ch:
                    kw["channel"] = ch
                context = p.chromium.launch_persistent_context(**kw)
                break
            except Exception as e:
                last_err = e
                continue
        if context is None:
            raise RuntimeError(
                f"Không mở được Chrome profile đăng ký: {last_err}"
            )
        context.add_init_script(_STEALTH_INIT_SCRIPT)
        try:
            yield p, context
        finally:
            context.close()


@contextmanager
def playwright_cms_profile_browser(
    session: dict,
    *,
    headless: bool = False,
    channel: str | None = "chrome",
) -> Generator[tuple[Any, Any], None, None]:
    """
    Playwright persistent context trên profile CMS — KHÔNG chrome.exe + connect_over_cdp.

    Dùng sau warm cookie hoặc thay warm: cùng user-data-dir, tránh CF 475 do CDP reconnect.
    """
    if _in_running_asyncio():
        raise RuntimeError(
            "playwright_cms_profile_browser không dùng trong asyncio loop; "
            "dùng run_playwright_thread(...)."
        )
    _playwright_thread_setup()
    from playwright.sync_api import sync_playwright

    proxy_str = ensure_proxy(session, explicit_only=True)
    profile_dir = _register_profile_dir(proxy_str)
    profile_dir.mkdir(parents=True, exist_ok=True)
    px = playwright_proxy(proxy_str)

    launch_kw: dict[str, Any] = {
        "user_data_dir": str(profile_dir),
        "headless": headless,
        "locale": "vi-VN",
        "timezone_id": "Asia/Ho_Chi_Minh",
        "viewport": {"width": 1366, "height": 768},
        "args": [
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
        ],
        "ignore_default_args": ["--enable-automation"],
    }
    if px:
        launch_kw["proxy"] = px
    if session.get("user_agent"):
        launch_kw["user_agent"] = session["user_agent"]

    with sync_playwright() as p:
        context = None
        channels = [channel] if channel else []
        channels.extend(ch for ch in ("chrome", "msedge", None) if ch not in channels)
        last_err: Exception | None = None
        for ch in channels:
            try:
                kw = dict(launch_kw)
                if ch:
                    kw["channel"] = ch
                context = p.chromium.launch_persistent_context(**kw)
                break
            except Exception as e:
                last_err = e
                continue
        if context is None:
            raise RuntimeError(f"Không mở được profile CMS Playwright: {last_err}")
        context.add_init_script(_STEALTH_INIT_SCRIPT)
        try:
            yield p, context
        finally:
            context.close()
