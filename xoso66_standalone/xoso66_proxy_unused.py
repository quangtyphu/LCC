# -*- coding: utf-8 -*-
"""
So sánh proxy proxy.vn với DB tài khoản XOSO66 — in proxy CHƯA GẮN acc.

Chạy (từ thư mục xoso66_standalone hoặc LC79):
  python xoso66_proxy_unused.py [09xxx matkhau]
  python xoso66_proxy_unused.py --all          # Cả HTTP (mặc định chỉ SOCKS5)
  python xoso66_proxy_unused.py --api          # Đọc proxy đã dùng qua API thay vì SQLite
  python xoso66_proxy_unused.py --show-used    # In thêm toàn bộ proxy đã gắn từng acc
  python xoso66_proxy_unused.py --db-only        # Chỉ kiểm tra DB (proxy trùng), không cần proxy.vn

Cần: proxy_vn_cookies.txt hoặc đăng nhập user/pass (dùng chung file ở thư mục LC79).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import importlib.util

_STANDALONE = Path(__file__).resolve().parent
_REPO_ROOT = _STANDALONE.parent
if str(_STANDALONE) not in sys.path:
    sys.path.insert(0, str(_STANDALONE))


def _load_lc79_proxy_unused():
    """Import LC79/proxy_unused.py (tránh trùng tên file trong xoso66_standalone)."""
    path = _REPO_ROOT / "proxy_unused.py"
    if not path.is_file():
        raise FileNotFoundError(f"Không thấy {path}")
    spec = importlib.util.spec_from_file_location("lc79_proxy_unused", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Không load được {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_pu = _load_lc79_proxy_unused()
PROXY_LIST_FILE = _pu.PROXY_LIST_FILE
get_proxies_from_web = _pu.get_proxies_from_web
login_http = _pu.login_http
normalize_proxy = _pu.normalize_proxy

# File cookie/list cùng LC79 (thư mục gốc repo)
os.chdir(_REPO_ROOT)


def get_proxies_from_xoso66_db() -> list[tuple[str, str, str]]:
    """(username, proxy, status) từ SQLite xoso66.db."""
    from xoso66_accounts_db import DB_PATH, init_db, list_accounts

    init_db()
    rows = list_accounts()
    out: list[tuple[str, str, str]] = []
    for r in rows:
        proxy = str(r.get("proxy") or "").strip()
        if not proxy:
            continue
        out.append(
            (
                str(r.get("username") or r.get("id") or "").strip(),
                proxy,
                str(r.get("status") or "").strip(),
            )
        )
    return out, str(DB_PATH)


def get_proxies_from_xoso66_api() -> list[tuple[str, str, str]]:
    """(username, proxy, status) qua GET /api/accounts."""
    import requests

    from xoso66_config_util import load_config

    cfg = load_config()
    host = str(cfg.get("api_host") or "127.0.0.1").strip() or "127.0.0.1"
    port = int(cfg.get("api_port") or 8799)
    key = str(cfg.get("api_key") or "").strip()
    url = f"http://{host}:{port}/api/accounts"
    headers = {"X-API-Key": key} if key else {}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    rows = resp.json()
    out: list[tuple[str, str, str]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        proxy = str(r.get("proxy") or "").strip()
        if not proxy:
            continue
        out.append(
            (
                str(r.get("username") or r.get("id") or "").strip(),
                proxy,
                str(r.get("status") or "").strip(),
            )
        )
    return out, url


def _build_used_index(
    used_rows: list[tuple[str, str, str]],
) -> dict[str, list[tuple[str, str, str]]]:
    """key = proxy chuẩn hóa → danh sách (username, proxy gốc, status)."""
    used_norm: dict[str, list[tuple[str, str, str]]] = {}
    for user, proxy, status in used_rows:
        key = normalize_proxy(proxy)
        if not key:
            continue
        used_norm.setdefault(key, []).append((user, proxy, status))
    return used_norm


def _print_duplicate_proxies(used_norm: dict[str, list[tuple[str, str, str]]]) -> None:
    dup = {k: v for k, v in used_norm.items() if len(v) > 1}
    acc_dup = sum(len(v) for v in dup.values())
    print(
        f"\n⚠️ Proxy trong DB gắn ≥2 acc "
        f"({len(dup)} proxy, {acc_dup} acc liên quan):\n"
    )
    if not dup:
        print("(Không có — mỗi proxy trong DB chỉ 1 acc)\n")
        return
    for _key, items in sorted(dup.items(), key=lambda x: (-len(x[1]), x[1][0][1])):
        print(f"  [{len(items)} acc] {items[0][1]}")
        for user, _proxy, status in sorted(items, key=lambda x: (x[0] or "").lower()):
            st = f" — {status}" if status else ""
            print(f"      · {user or '?'}{st}")
        print()


def _print_unused_proxies(
    proxies_web: list[str], used_norm: dict[str, list[tuple[str, str, str]]]
) -> None:
    used_set = set(used_norm.keys())
    unused = [
        p
        for p in proxies_web
        if normalize_proxy(p) and normalize_proxy(p) not in used_set
    ]
    print(
        f"\n🔒 Proxy proxy.vn CHƯA có trong DB ({len(unused)}/{len(proxies_web)}):\n"
    )
    if not unused:
        print("(Không có — mọi proxy trên web đã có ít nhất 1 acc trong DB)\n")
        return
    for p in unused:
        print(p)
    print()


def main() -> None:
    argv = sys.argv[1:]
    socks5_only = "--all" not in argv
    show_used = "--show-used" in argv
    use_api = "--api" in argv
    db_only = "--db-only" in argv
    args = [a for a in argv if not a.startswith("--")]

    username = (
        os.environ.get("PROXY_VN_USER", "").strip()
        or os.environ.get("PROXY_VN_PHONE", "").strip()
    )
    password = os.environ.get("PROXY_VN_PASS", "").strip()
    if len(args) >= 2:
        username, password = args[0], args[1]
    elif len(args) == 1:
        password = args[0]
        username = input("Tên đăng nhập proxy.vn: ").strip()

    proxies_web: list[str] = []
    from_cache = False

    if not db_only:
        if username and password:
            print("Đang đăng nhập proxy.vn...", end=" ")
            cookies = login_http(username, password)
            if not cookies:
                print("❌")
                print("Không đăng nhập được proxy.vn — kiểm tra user/pass.")
                sys.exit(1)
            print("OK")

        proxies_web, from_cache = get_proxies_from_web(socks5_only=socks5_only)
        if not proxies_web:
            print("❌ Không có proxy từ proxy.vn.")
            print("   Chạy: python xoso66_proxy_unused.py 09xxx matkhau")
            print("   Hoặc: python xoso66_proxy_unused.py --db-only")
            sys.exit(1)

        if from_cache:
            print(
                f"⚠️ Đang dùng {PROXY_LIST_FILE} (có thể cũ). "
                "Chạy với user/pass để lấy danh sách mới.\n"
            )

    try:
        if use_api:
            used_rows, src = get_proxies_from_xoso66_api()
            src_label = f"API {src}"
        else:
            used_rows, src = get_proxies_from_xoso66_db()
            src_label = f"DB {src}"
    except Exception as e:
        print(f"❌ Không đọc được proxy XOSO66 ({'API' if use_api else 'DB'}): {e}")
        if not use_api:
            print("   Thử: python xoso66_proxy_unused.py --api (cần main.py + API bật)")
        sys.exit(1)

    used_norm = _build_used_index(used_rows)
    unique_proxy_n = len(used_norm)

    print(f"Nguồn XOSO66: {src_label}")
    print(f"Acc có proxy trong DB: {len(used_rows)}")
    print(f"Proxy khác nhau trong DB (sau chuẩn hóa): {unique_proxy_n}")
    if not db_only:
        print(f"Proxy proxy.vn (web): {len(proxies_web)}")

    _print_duplicate_proxies(used_norm)

    if not db_only:
        _print_unused_proxies(proxies_web, used_norm)

    if show_used and used_rows:
        print(f"\n📋 Proxy đã gắn ({len(used_rows)} acc):\n")
        for user, proxy, status in sorted(used_rows, key=lambda x: (x[1], x[0])):
            st = f" [{status}]" if status else ""
            print(f"  {user or '?'}{st}\t{proxy}")


if __name__ == "__main__":
    main()
