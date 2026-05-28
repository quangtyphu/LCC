# -*- coding: utf-8 -*-
"""
Proxy SOCKS5 LC79 — format host:port:user:pass (bảng user_profiles, game_data.db).

Playwright/Chromium không auth SOCKS5 trực tiếp → relay HTTP local (pproxy).
"""
from __future__ import annotations

import atexit
import os
import random
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path(
    os.environ.get("LC79_GAME_DATA_DB", r"C:\Users\Quang\Documents\CMS\game_data.db")
)

_relays: dict[str, subprocess.Popen] = {}
_ports: dict[str, int] = {}


def parse_proxy(proxy_str: str) -> tuple[str, int, str, str]:
    s = (proxy_str or "").strip()
    for prefix in ("socks5h://", "socks5://", "http://", "https://"):
        if s.lower().startswith(prefix):
            s = s[len(prefix) :]
    parts = s.split(":")
    if len(parts) == 4:
        host, port_s, user, pwd = parts
        return host.strip(), int(port_s), user.strip(), pwd.strip()
    if len(parts) == 2:
        return parts[0].strip(), int(parts[1]), "", ""
    raise ValueError(
        'proxy phải "host:port:user:pass" hoặc "host:port", nhận: ' + repr(proxy_str)
    )


def proxy_has_auth(proxy_str: str) -> bool:
    try:
        _, _, user, _ = parse_proxy(proxy_str)
        return bool(user)
    except ValueError:
        return False


def proxy_log_label(proxy_str: str) -> str:
    try:
        host, port, user, _ = parse_proxy(proxy_str)
        if user:
            return f"{host}:{port} (socks5, user {user})"
        return f"{host}:{port} (socks5)"
    except Exception:
        s = (proxy_str or "").strip()
        return (s[:48] + "…") if len(s) > 48 else s or "(không có proxy)"


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _socks_remote_url(proxy_str: str) -> str:
    """URI remote cho pproxy -r (chỉ socks5://, không socks5h — pproxy 2.7 lỗi cú pháp)."""
    host, port, user, pwd = parse_proxy(proxy_str)
    if user:
        return f"socks5://{host}:{port}#{user}:{pwd}"
    return f"socks5://{host}:{port}"


def _relay_alive(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.2):
            return True
    except OSError:
        return False


def _stop_relay(key: str) -> None:
    proc = _relays.pop(key, None)
    _ports.pop(key, None)
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            pass


def ensure_local_http_relay(proxy_str: str, *, force_restart: bool = False) -> str:
    key = proxy_str.strip()
    if force_restart:
        _stop_relay(key)

    proc = _relays.get(key)
    port = _ports.get(key)
    if proc is not None and proc.poll() is None and port and _relay_alive(port):
        return f"http://127.0.0.1:{port}"

    if proc is not None and proc.poll() is None:
        _stop_relay(key)

    try:
        import pproxy  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "Cần pproxy cho SOCKS5 có user/pass + Playwright: pip install pproxy"
        ) from e

    last_err = ""
    for attempt in range(2):
        local_port = _free_port()
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "pproxy",
                "-l",
                f"http://127.0.0.1:{local_port}/",
                "-r",
                _socks_remote_url(key),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for _ in range(20):
            time.sleep(0.25)
            if proc.poll() is not None:
                break
            if _relay_alive(local_port):
                break
        if proc.poll() is not None:
            err = ""
            if proc.stderr:
                try:
                    raw = proc.stderr.read().decode("utf-8", errors="replace")
                    lines = [
                        ln
                        for ln in raw.splitlines()
                        if "usage:" not in ln.lower()
                        and "pkg_resources" not in ln
                        and "UserWarning" not in ln
                    ]
                    err = "\n".join(lines).strip()[:500]
                except Exception:
                    pass
            last_err = err or "pproxy thoát sớm"
            continue
        if not _relay_alive(local_port):
            last_err = "relay local không lên port"
            try:
                proc.terminate()
            except Exception:
                pass
            continue
        _relays[key] = proc
        _ports[key] = local_port
        return f"http://127.0.0.1:{local_port}"

    raise RuntimeError(f"Không khởi động relay pproxy: {last_err}")


def test_http_via_proxy(
    proxy_server: str,
    test_url: str = "https://c1686.net/home/login",
    timeout: float = 18.0,
) -> tuple[bool, str]:
    """Thử GET qua --proxy-server của Chrome (http://127.0.0.1:… hoặc socks5://…)."""
    import urllib.error
    import urllib.request

    if not (proxy_server or "").strip():
        return True, "không dùng proxy"
    handler = urllib.request.ProxyHandler(
        {"http": proxy_server, "https": proxy_server}
    )
    opener = urllib.request.build_opener(handler)
    try:
        with opener.open(test_url, timeout=timeout) as resp:
            return True, f"HTTP {getattr(resp, 'status', '?')}"
    except urllib.error.HTTPError as e:
        if e.code and int(e.code) < 500:
            return True, f"HTTP {e.code}"
        return False, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return False, str(e)[:220]


def verify_proxy_chain(
    proxy_str: str,
    *,
    test_url: str = "https://c1686.net/home/login",
    timeout: float = 18.0,
) -> tuple[bool, str]:
    """
    Kiểm tra SOCKS5 + relay pproxy trước khi mở Chrome.
    Chrome chỉ đi một đường proxy — relay chết = trình duyệt “mất mạng”.
    """
    p = (proxy_str or "").strip()
    if not p:
        return True, "không dùng proxy"
    try:
        srv = chrome_proxy_server(p)
    except Exception as e:
        return False, f"relay: {e}"
    ok, detail = test_http_via_proxy(srv, test_url=test_url, timeout=timeout)
    if ok:
        return True, f"{proxy_log_label(p)} → {detail}"
    _stop_relay(p)
    try:
        srv = chrome_proxy_server(p)
        ok2, detail2 = test_http_via_proxy(srv, test_url=test_url, timeout=timeout)
        if ok2:
            return True, f"{proxy_log_label(p)} (relay mới) → {detail2}"
        return False, detail2
    except Exception as e:
        return False, f"{detail}; relay lại: {e}"


def stop_all_relays() -> None:
    for proc in list(_relays.values()):
        if proc.poll() is None:
            proc.terminate()
    _relays.clear()
    _ports.clear()


def stop_relays_unless_capture_chrome() -> None:
    """Script thoát nhưng Chrome capture còn mở → giữ relay (tránh Chrome mất mạng)."""
    try:
        from c168_capture_game_b import _cdp_alive

        if _cdp_alive():
            return
    except Exception:
        pass
    stop_all_relays()


atexit.register(stop_relays_unless_capture_chrome)


def playwright_proxy_dict(proxy_str: str) -> dict[str, str]:
    """Dict cho browser.new_context(proxy=...)."""
    if proxy_has_auth(proxy_str):
        return {"server": ensure_local_http_relay(proxy_str)}
    host, port, _, _ = parse_proxy(proxy_str)
    return {"server": f"socks5://{host}:{port}"}


def websocket_run_forever_kwargs(proxy_server: str) -> dict[str, Any]:
    """
    Tham số proxy cho websocket.WebSocketApp.run_forever.
    Bắt buộc proxy_type rõ ràng (None → lỗi 'Only http, socks4, socks5…').
    """
    from urllib.parse import unquote, urlparse

    p = (proxy_server or "").strip()
    if not p:
        return {}
    if p.startswith("http://") or p.startswith("https://"):
        pu = urlparse(p)
        host = pu.hostname or ""
        if not host:
            return {}
        kw: dict[str, Any] = {
            "http_proxy_host": host,
            "http_proxy_port": int(pu.port or (443 if pu.scheme == "https" else 80)),
            "proxy_type": "http",
        }
        if pu.username:
            kw["http_proxy_auth"] = (unquote(pu.username), unquote(pu.password or ""))
        return kw
    if p.lower().startswith(("socks5://", "socks5h://")):
        pu = urlparse(p)
        host = pu.hostname or ""
        if not host:
            return {}
        kw = {
            "http_proxy_host": host,
            "http_proxy_port": int(pu.port or 1080),
            "proxy_type": "socks5h" if p.lower().startswith("socks5h") else "socks5",
        }
        if pu.username:
            kw["http_proxy_auth"] = (unquote(pu.username), unquote(pu.password or ""))
        return kw
    try:
        return websocket_run_forever_kwargs(ensure_local_http_relay(p))
    except Exception:
        return {}


def chrome_proxy_server(proxy_str: str) -> str:
    """URL cho Chrome --proxy-server= (SOCKS5 auth → relay HTTP local)."""
    p = (proxy_str or "").strip()
    if not p:
        return ""
    if proxy_has_auth(p):
        return ensure_local_http_relay(p)
    host, port, _, _ = parse_proxy(p)
    return f"socks5://{host}:{port}"


def list_proxies_prioritized_from_lc79_db(db_path: str | Path | None = None) -> list[str]:
    """
    Proxy từ user_profiles — ưu tiên acc status 'Đang Chơi' (đang dùng ổn trên LC79).
    """
    path = Path(db_path or DEFAULT_DB_PATH)
    if not path.is_file():
        raise FileNotFoundError(f"Không thấy DB LC79: {path}")
    active: list[str] = []
    other: list[str] = []
    conn = sqlite3.connect(str(path))
    try:
        cur = conn.execute(
            """
            SELECT TRIM(proxy) AS p, TRIM(COALESCE(status, '')) AS st
            FROM user_profiles
            WHERE proxy IS NOT NULL AND TRIM(proxy) != ''
            """
        )
        for p, st in cur.fetchall():
            s = str(p or "").strip()
            if not s:
                continue
            try:
                parse_proxy(s)
            except ValueError:
                continue
            if str(st) == "Đang Chơi":
                active.append(s)
            else:
                other.append(s)
    finally:
        conn.close()
    random.shuffle(active)
    random.shuffle(other)
    seen: set[str] = set()
    out: list[str] = []
    for s in active + other:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def list_proxies_from_lc79_db(db_path: str | Path | None = None) -> list[str]:
    path = Path(db_path or DEFAULT_DB_PATH)
    if not path.is_file():
        raise FileNotFoundError(f"Không thấy DB LC79: {path}")
    conn = sqlite3.connect(str(path))
    try:
        cur = conn.execute(
            """
            SELECT DISTINCT TRIM(proxy) AS p
            FROM user_profiles
            WHERE proxy IS NOT NULL AND TRIM(proxy) != ''
            """
        )
        out: list[str] = []
        for (p,) in cur.fetchall():
            s = str(p or "").strip()
            if not s:
                continue
            try:
                parse_proxy(s)
                out.append(s)
            except ValueError:
                continue
        return out
    finally:
        conn.close()


def pick_random_proxy_from_db(db_path: str | Path | None = None) -> str:
    proxies = list_proxies_prioritized_from_lc79_db(db_path)
    if not proxies:
        raise RuntimeError("DB LC79 không có proxy SOCKS5 nào trong user_profiles")
    return random.choice(proxies)


def resolve_register_proxy(
    *,
    explicit: str = "",
    cfg: dict[str, Any] | None = None,
    use_db: bool = True,
) -> str:
    p = (explicit or "").strip()
    if p:
        parse_proxy(p)
        return p
    proxy_cfg = (cfg or {}).get("proxy") if isinstance((cfg or {}).get("proxy"), dict) else {}
    if proxy_cfg.get("enabled") is False:
        return ""
    if not use_db:
        return ""
    db_path = proxy_cfg.get("db_path") or DEFAULT_DB_PATH
    return pick_random_proxy_from_db(db_path)
