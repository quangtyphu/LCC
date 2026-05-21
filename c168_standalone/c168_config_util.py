# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parent
CONFIG_PATH = _DIR / "c168_config.json"
EXAMPLE_PATH = _DIR / "c168_config.example.json"

DEFAULTS: dict[str, Any] = {
    "base_url": "https://c168b2.cc",
    "api_base_url": "",
    "sitecode": "2865",
    "register_path": "/home/register",
    "captcha": {
        "mode": "browser",
        "kind": "turnstile",
        "provider": "capsolver",
        "api_key": "",
        "captcha_id": "62c528ead784206de7e6db17765b9ac0",
        "custom_url": os.environ.get("C168_CAPTCHA_SOLVER_URL", "http://127.0.0.1:9999/solve"),
        "sitekey": "0x4AAAAAABhSPiw6QLnmnJMb",
        "pageurl": "https://c168b2.cc/home/register",
        "timeout_sec": 120,
        "gt": "",
        "challenge": "",
    },
    "playwright": {
        "headless": True,
        "timeout_ms": 120_000,
        "human_delay_ms": 120,
        "pause_between_fields_ms": 500,
        "pause_before_submit_ms": 2000,
    },
    "proxy": {
        "enabled": True,
        "from_db": True,
        "db_path": r"C:\Users\Quang\Documents\CMS\game_data.db",
    },
}


def load_config() -> dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULTS))
    path = CONFIG_PATH if CONFIG_PATH.is_file() else EXAMPLE_PATH
    if path.is_file():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            _deep_merge(cfg, raw)
    if os.environ.get("C168_BASE_URL"):
        cfg["base_url"] = os.environ["C168_BASE_URL"].rstrip("/")
    if os.environ.get("C168_CAPTCHA_API_KEY"):
        cfg.setdefault("captcha", {})["api_key"] = os.environ["C168_CAPTCHA_API_KEY"]
    if os.environ.get("C168_CAPTCHA_PROVIDER"):
        cfg.setdefault("captcha", {})["provider"] = os.environ["C168_CAPTCHA_PROVIDER"]
    cap = cfg.get("captcha") if isinstance(cfg.get("captcha"), dict) else {}
    if not cap.get("pageurl"):
        cap["pageurl"] = cfg["base_url"].rstrip("/") + cfg.get("register_path", "/home/register")
    cfg["captcha"] = cap
    return cfg


def _deep_merge(base: dict, patch: dict) -> None:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
