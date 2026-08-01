"""
Đăng nhập proxy.vn → lấy toàn bộ proxy → kiểm tra sống/chết → in kết quả.

Chạy:
  python proxy_check_live.py 0972333999 Valentine1
  python proxy_check_live.py --all          # Cả HTTP, không chỉ SOCKS5
  python proxy_check_live.py --file-only    # Chỉ check proxy_list.txt (không cần đăng nhập)
  python proxy_check_live.py --workers 20   # Số luồng check song song (mặc định 15)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from proxy_unused import (  # noqa: E402
    COOKIES_FILE,
    PROXY_LIST_FILE,
    fetch_proxy_list,
    get_proxies_from_web,
    login_http,
)

# Host dùng để thử SOCKS5 (giống LC79 ws_connection)
SOCKS5_TEST_HOST = "wtx.tele68.com"
SOCKS5_TEST_PORT = 443
HTTP_TEST_URL = "http://httpbin.org/ip"


@dataclass
class ProxyRow:
    proxy: str
    ptype: str = ""
    live_web: str = ""


@dataclass
class CheckResult:
    proxy: str
    alive: bool
    detail: str
    ptype: str = ""
    live_web: str = ""


def _parse_proxy(proxy_str: str) -> tuple[str, int, str, str]:
    s = (proxy_str or "").strip()
    for prefix in ("socks5h://", "socks5://", "http://", "https://"):
        if s.lower().startswith(prefix):
            s = s[len(prefix) :]
    parts = s.split(":")
    if len(parts) >= 4:
        host, port_s, user = parts[0].strip(), parts[1].strip(), parts[2].strip()
        pwd = ":".join(parts[3:]).strip()
        return host, int(port_s), user, pwd
    if len(parts) == 2:
        return parts[0].strip(), int(parts[1]), "", ""
    raise ValueError(f"proxy format lỗi: {proxy_str!r}")


def _is_socks5(ptype: str, proxy: str) -> bool:
    t = (ptype or "").upper()
    if t == "SOCKS5":
        return True
    if t in ("HTTP", "HTTPS"):
        return False
    # Không có type từ web → đoán theo format host:port:user:pass
    try:
        _parse_proxy(proxy)
        return True
    except ValueError:
        return False


def check_socks5(proxy: str, timeout: float) -> tuple[bool, str]:
    import socks

    try:
        host, port, user, pwd = _parse_proxy(proxy)
    except ValueError as e:
        return False, str(e)

    sock = socks.socksocket()
    sock.settimeout(timeout)
    try:
        sock.set_proxy(socks.SOCKS5, host, port, True, user, pwd)
        sock.connect((SOCKS5_TEST_HOST, SOCKS5_TEST_PORT))
        return True, f"OK → {SOCKS5_TEST_HOST}:{SOCKS5_TEST_PORT}"
    except Exception as e:
        return False, str(e)[:180]
    finally:
        try:
            sock.close()
        except Exception:
            pass


def check_http(proxy: str, timeout: float) -> tuple[bool, str]:
    try:
        host, port, user, pwd = _parse_proxy(proxy)
    except ValueError as e:
        return False, str(e)

    if user:
        proxy_url = f"http://{user}:{pwd}@{host}:{port}"
    else:
        proxy_url = f"http://{host}:{port}"
    proxies = {"http": proxy_url, "https": proxy_url}
    try:
        r = requests.get(HTTP_TEST_URL, proxies=proxies, timeout=timeout)
        if r.status_code < 500:
            return True, f"HTTP {r.status_code}"
        return False, f"HTTP {r.status_code}"
    except Exception as e:
        return False, str(e)[:180]


def check_one(row: ProxyRow, timeout: float) -> CheckResult:
    if _is_socks5(row.ptype, row.proxy):
        ok, detail = check_socks5(row.proxy, timeout)
        kind = "SOCKS5"
    else:
        ok, detail = check_http(row.proxy, timeout)
        kind = row.ptype or "HTTP"
    return CheckResult(
        proxy=row.proxy,
        alive=ok,
        detail=f"{kind}: {detail}",
        ptype=row.ptype,
        live_web=row.live_web,
    )


def load_rows_from_web(socks5_only: bool) -> tuple[list[ProxyRow], bool]:
    proxies, from_cache = get_proxies_from_web(socks5_only=socks5_only)
    if not proxies:
        return [], from_cache

    cookies: dict[str, str] = {}
    try:
        with open(COOKIES_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    cookies[k.strip()] = v.strip()
    except FileNotFoundError:
        pass

    rows: list[ProxyRow] = []
    if cookies:
        try:
            web_rows = fetch_proxy_list(cookies)
            for r in web_rows:
                proxy = (r.get("proxy_trung_gian") or r.get("proxy_goc") or "").strip()
                ptype = (r.get("type") or "").strip()
                live_web = (r.get("live") or "").strip()
                if not proxy:
                    continue
                if socks5_only and ptype.upper() != "SOCKS5":
                    continue
                rows.append(ProxyRow(proxy=proxy, ptype=ptype, live_web=live_web))
            if rows:
                return rows, from_cache
        except Exception:
            pass

    for p in proxies:
        rows.append(ProxyRow(proxy=p))
    return rows, from_cache


def load_rows_from_file() -> list[ProxyRow]:
    path = SCRIPT_DIR / PROXY_LIST_FILE
    if not path.is_file():
        return []
    lines = [
        ln.strip()
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    return [ProxyRow(proxy=p) for p in lines]


def run_checks(
    rows: list[ProxyRow],
    *,
    workers: int,
    timeout: float,
) -> list[CheckResult]:
    results: list[CheckResult] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(check_one, row, timeout): row for row in rows}
        for fut in as_completed(futures):
            results.append(fut.result())
    results.sort(key=lambda r: r.proxy)
    return results


def print_report(results: list[CheckResult], elapsed: float) -> None:
    alive = [r for r in results if r.alive]
    dead = [r for r in results if not r.alive]

    print(f"\n{'=' * 60}")
    print(f"Tổng: {len(results)} | ✅ Sống: {len(alive)} | ❌ Chết: {len(dead)} | {elapsed:.1f}s")
    print(f"{'=' * 60}")

    print(f"\n✅ PROXY SỐNG ({len(alive)}):\n")
    if alive:
        for r in alive:
            extra = ""
            if r.live_web:
                extra = f"  [web Live={r.live_web}]"
            print(f"  {r.proxy}{extra}")
    else:
        print("  (không có)")

    print(f"\n❌ PROXY CHẾT ({len(dead)}):\n")
    if dead:
        for r in dead:
            extra = ""
            if r.live_web:
                extra = f"  [web Live={r.live_web}]"
            print(f"  {r.proxy}  —  {r.detail}{extra}")
    else:
        print("  (không có)")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Check proxy sống/chết từ proxy.vn")
    p.add_argument("username", nargs="?", help="Tài khoản proxy.vn (SĐT)")
    p.add_argument("password", nargs="?", help="Mật khẩu proxy.vn")
    p.add_argument(
        "--all",
        action="store_true",
        help="Lấy cả HTTP, không chỉ SOCKS5",
    )
    p.add_argument(
        "--file-only",
        action="store_true",
        help="Chỉ đọc proxy_list.txt, không đăng nhập proxy.vn",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=15,
        help="Số luồng check song song (mặc định 15)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=8.0,
        help="Timeout mỗi proxy (giây, mặc định 8)",
    )
    return p.parse_args()


def main() -> None:
    os.chdir(SCRIPT_DIR)
    args = parse_args()
    socks5_only = not args.all

    username = (
        os.environ.get("PROXY_VN_USER", "").strip()
        or os.environ.get("PROXY_VN_PHONE", "").strip()
    )
    password = os.environ.get("PROXY_VN_PASS", "").strip()
    if args.username:
        username = args.username
    if args.password:
        password = args.password

    rows: list[ProxyRow] = []
    from_cache = False

    if args.file_only:
        rows = load_rows_from_file()
        if not rows:
            print(f"❌ Không có proxy trong {PROXY_LIST_FILE}")
            sys.exit(1)
        print(f"Đọc {len(rows)} proxy từ {PROXY_LIST_FILE}")
    else:
        if username and password:
            print("Đang đăng nhập proxy.vn...", end=" ")
            cookies = login_http(username, password)
            if not cookies:
                print("❌")
                print("Không đăng nhập được. Kiểm tra tài khoản/mật khẩu.")
                sys.exit(1)
            print("OK")

        rows, from_cache = load_rows_from_web(socks5_only=socks5_only)
        if not rows:
            print("❌ Không lấy được proxy từ proxy.vn.")
            print("   Chạy: python proxy_check_live.py 09xxx matkhau")
            sys.exit(1)
        if from_cache:
            print(f"⚠️ Đang dùng {PROXY_LIST_FILE} (có thể cũ). Chạy với user/pass để lấy mới.")

    print(
        f"Đang check {len(rows)} proxy "
        f"({'SOCKS5 only' if socks5_only else 'tất cả loại'}, "
        f"{args.workers} luồng, timeout {args.timeout}s)..."
    )
    t0 = time.time()
    results = run_checks(rows, workers=args.workers, timeout=args.timeout)
    print_report(results, time.time() - t0)


if __name__ == "__main__":
    main()
