# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parent
CONFIG_PATH = _DIR / "allgame_config.json"
EXAMPLE_PATH = _DIR / "allgame_config.example.json"

DEFAULTS: dict[str, Any] = {
    "reconcile_interval_sec": 60,
    "chrome_max_concurrent": 10,
    "daily_bet_cap_vnd": 890_000,
    "watcher_enabled": True,
    "vendor": {
        "platform_id": 1012,
        "category_id": 4,
        "table_name": "C06",
        "table_id": 1006,
    },
}


def load_config() -> dict[str, Any]:
    path = Path(os.environ.get("ALLGAME_CONFIG", CONFIG_PATH))
    cfg = dict(DEFAULTS)
    if path.is_file():
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                cfg.update(raw)
                if isinstance(raw.get("vendor"), dict):
                    cfg["vendor"] = {**DEFAULTS["vendor"], **raw["vendor"]}
        except Exception as e:
            print(f"[ALLGAME] Lỗi đọc config {path}: {e}", flush=True)
    return cfg
