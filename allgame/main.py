#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ALLGAME — nhiều cổng game → chung Game B (BCR).

  cd allgame
  python main.py
  python main.py --init-db
  python main.py --reconcile-once

DB mặc định: ../CMS/game_data/allgame.db
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_REPO = _ROOT.parent
os.chdir(_ROOT)
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

os.environ.setdefault("PYTHONUNBUFFERED", "1")

from allgame.config_util import load_config
from allgame.db import init_db, init_portals
from allgame.db.accounts_db import DB_PATH
from allgame.portals.registry import bootstrap_portals, list_registered_portal_ids
from allgame.orchestrator.reconcile import reconcile_once
from allgame.orchestrator.watcher import request_stop, run_watcher, stopping


def _init_storage() -> None:
    init_db()
    init_portals(seed_defaults=True)
    bootstrap_portals()
    print(f"[ALLGAME] DB: {DB_PATH}", flush=True)
    print(f"[ALLGAME] Portals registered: {list_registered_portal_ids()}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="ALLGAME orchestrator")
    ap.add_argument("--init-db", action="store_true", help="Tạo bảng + seed portals rồi thoát")
    ap.add_argument("--reconcile-once", action="store_true", help="Chạy một vòng reconcile")
    args = ap.parse_args()

    _init_storage()

    if args.init_db:
        return 0

    if args.reconcile_once:
        report = reconcile_once()
        print(report, flush=True)
        return 0

    cfg = load_config()
    if cfg.get("watcher_enabled", True):
        t = threading.Thread(target=run_watcher, daemon=True, name="allgame-watcher")
        t.start()
        print("[ALLGAME] Watcher started (Ctrl+C to stop)", flush=True)
        try:
            while not stopping():
                t.join(timeout=1.0)
        except KeyboardInterrupt:
            request_stop()
            print("[ALLGAME] Stopping...", flush=True)
    else:
        print("[ALLGAME] watcher_enabled=false — không chạy nền", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
