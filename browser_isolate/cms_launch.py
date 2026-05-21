# -*- coding: utf-8 -*-
"""Mở Chrome detached cho CMS (không chặn Node server)."""
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

from launcher import _launch_chrome_native, _normalize_urls  # noqa: E402
from proxy_util import prepare_chrome_proxy  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile-dir", required=True)
    ap.add_argument("--proxy", default="")
    ap.add_argument("--url", default="about:blank", help="Một URL (cũ, tương thích CMS)")
    ap.add_argument(
        "--urls-json",
        default="",
        help='JSON array URL, VD: ["https://a.com","https://b.com"]',
    )
    args = ap.parse_args()
    proxy_raw = args.proxy.strip()
    url_list: list[str] = []
    if args.urls_json.strip():
        try:
            parsed = json.loads(args.urls_json)
            if isinstance(parsed, list):
                url_list = [str(x) for x in parsed]
        except json.JSONDecodeError as e:
            print(json.dumps({"ok": False, "error": f"urls-json không hợp lệ: {e}"}))
            return 1
    try:
        prep = prepare_chrome_proxy(proxy_raw) if proxy_raw else {
            "mode": "none",
            "server": "",
            "label": "(không proxy)",
        }
        proc = _launch_chrome_native(
            profile_path=Path(args.profile_dir),
            proxy=proxy_raw,
            url=args.url.strip() or "about:blank",
            urls=url_list or None,
        )
        opened = _normalize_urls(url_list, args.url.strip() or "about:blank")
        print(
            json.dumps(
                {
                    "ok": True,
                    "pid": proc.pid,
                    "proxy_mode": prep.get("mode"),
                    "proxy_label": prep.get("label"),
                    "tabs": len(opened),
                    "urls": opened,
                },
                ensure_ascii=False,
            )
        )
        return 0
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
