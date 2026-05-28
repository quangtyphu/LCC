# -*- coding: utf-8 -*-
"""Đã gộp vào c168_listen_ws.py — giữ file này để import cũ không vỡ."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from c168_listen_ws import ListenParams, listen_vendor_ws


def run_listen(
    duration_s: int,
    *,
    cdp_base: str = "",
    log_path: Path | None = None,
    verbose: bool = False,  # noqa: ARG001 — không dùng
    sniffer: Any | None = None,
    table_id: int = 0,
    username: str = "",
) -> int:
    r = listen_vendor_ws(
        ListenParams(
            username=username,
            cdp_url=cdp_base,
            table_id=int(table_id or 0),
            duration_sec=int(duration_s),
            log_path=log_path,
            sniffer=sniffer,
        )
    )
    return r.exit_code


def main() -> int:
    import argparse

    from c168_capture_game_b import CDP_URL
    from c168_vendor_enter_table import DEFAULT_TABLE_ID

    ap = argparse.ArgumentParser(
        description="(alias) dùng c168_listen_ws.py — nghe WS vendor"
    )
    ap.add_argument("duration", nargs="?", type=int, default=300)
    ap.add_argument("--cdp", default=CDP_URL)
    ap.add_argument("--log", default="")
    ap.add_argument("-u", "--username", default="")
    ap.add_argument("--table-id", type=int, default=DEFAULT_TABLE_ID)
    args = ap.parse_args()
    print("Gợi ý: python c168_listen_ws.py -u USER --table-id 1006", flush=True)
    return run_listen(
        args.duration,
        cdp_base=args.cdp.rstrip("/"),
        log_path=Path(args.log) if args.log else None,
        table_id=int(args.table_id or 0),
        username=args.username.strip(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
