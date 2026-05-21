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

from launcher import _launch_chrome_native  # noqa: E402
from proxy_util import prepare_chrome_proxy  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile-dir", required=True)
    ap.add_argument("--proxy", default="")
    ap.add_argument("--url", default="about:blank")
    args = ap.parse_args()
    proxy_raw = args.proxy.strip()
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
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "pid": proc.pid,
                    "proxy_mode": prep.get("mode"),
                    "proxy_label": prep.get("label"),
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
