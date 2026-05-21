# -*- coding: utf-8 -*-
"""
Browser isolate — mỗi lần mở = profile + proxy riêng (kiểu GoLogin đơn giản).

Ví dụ:
  python main.py new --url https://example.com
  python main.py new --proxy 1.2.3.4:1080:user:pass --url https://example.com
  python main.py new --proxy-file proxies.txt
  python main.py new --ephemeral
  python main.py open abc12345
  python main.py list
  python main.py delete abc12345
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

from launcher import open_session
from proxy_util import pick_from_file, proxy_label
from session_store import delete_session, list_sessions, new_session_id

_DIR = Path(__file__).resolve().parent


def cmd_list(_: argparse.Namespace) -> int:
    rows = list_sessions()
    if not rows:
        print("Chưa có session nào (chạy: python main.py new ...)")
        return 0
    for r in rows:
        print(
            f"{r.get('id')}  proxy={proxy_label(str(r.get('proxy') or ''))}  "
            f"ephemeral={r.get('ephemeral')}  {r.get('created', '')}"
        )
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    if delete_session(args.session_id):
        print(f"Đã xóa session {args.session_id}")
        return 0
    print(f"Không tìm thấy session {args.session_id}", file=sys.stderr)
    return 1


def _resolve_proxy(args: argparse.Namespace) -> str:
    if args.proxy:
        return args.proxy.strip()
    if args.proxy_file:
        return pick_from_file(Path(args.proxy_file))
    return ""


def cmd_new(args: argparse.Namespace) -> int:
    proxy = _resolve_proxy(args)
    sid = new_session_id() if not args.session_id else args.session_id
    out = open_session(
        proxy=proxy,
        url=args.url,
        ephemeral=args.ephemeral,
        session_id=sid,
        reuse=False,
        engine=args.engine,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


def cmd_open(args: argparse.Namespace) -> int:
    proxy = args.proxy.strip() if args.proxy else ""
    out = open_session(
        proxy=proxy,
        url=args.url,
        ephemeral=False,
        session_id=args.session_id,
        reuse=True,
        engine=args.engine,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Chrome profile + proxy per session (simple anti-detect)"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_new = sub.add_parser("new", help="Mở session mới (profile sạch hoặc ephemeral)")
    p_new.add_argument("--url", default="about:blank")
    p_new.add_argument("--proxy", default="", help="host:port:user:pass hoặc host:port")
    p_new.add_argument("--proxy-file", default="", help="File danh sách proxy, chọn ngẫu nhiên")
    p_new.add_argument(
        "--ephemeral",
        action="store_true",
        help="Xóa profile sau khi đóng — mỗi lần như user mới hoàn toàn",
    )
    p_new.add_argument("--session-id", default="", help="Tùy chọn đặt id session")
    p_new.add_argument(
        "--engine",
        choices=("playwright", "chrome"),
        default="playwright",
        help="playwright=ổn định proxy SOCKS auth; chrome=chrome.exe thuần",
    )
    p_new.set_defaults(func=cmd_new)

    p_open = sub.add_parser("open", help="Mở lại session đã lưu (giữ cookie)")
    p_open.add_argument("session_id")
    p_open.add_argument("--url", default="about:blank")
    p_open.add_argument("--proxy", default="", help="Ghi đè proxy (tùy chọn)")
    p_open.add_argument("--engine", choices=("playwright", "chrome"), default="playwright")
    p_open.set_defaults(func=cmd_open)

    p_list = sub.add_parser("list", help="Liệt kê session")
    p_list.set_defaults(func=cmd_list)

    p_del = sub.add_parser("delete", help="Xóa session + profile")
    p_del.add_argument("session_id")
    p_del.set_defaults(func=cmd_delete)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
