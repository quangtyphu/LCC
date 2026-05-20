# -*- coding: utf-8 -*-
"""Lưu game đang chơi (auto_bet) — đọc lại khi khởi động."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parent
_DEFAULT_PATH = _DIR / "data" / "playing_game.json"
_LOCK = threading.Lock()


def _path() -> Path:
    raw = os.environ.get("XOSO66_PLAYING_GAME_FILE", "").strip()
    return Path(raw) if raw else _DEFAULT_PATH


def load_playing_game(*, max_age_sec: float = 1800) -> dict[str, Any] | None:
    p = _path()
    if not p.is_file():
        return None
    try:
        with _LOCK:
            data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("game_id") is None:
            return None
        updated = float(data.get("updated_at") or 0)
        if max_age_sec > 0 and updated and (time.time() - updated) > max_age_sec:
            return None
        return data
    except Exception:
        return None


def runtime_token_game_key(cfg: dict | None = None) -> str:
    """Game key dùng ping/refresh token — khớp auto_bet đang chơi."""
    try:
        from xoso66_auto_bet import get_auto_bet_controller

        k = str(get_auto_bet_controller().active_game_key() or "").strip()
        if k:
            return k
    except Exception:
        pass
    try:
        from xoso66_config_util import load_config

        cfg = cfg or load_config()
        ab = cfg.get("auto_bet") if isinstance(cfg.get("auto_bet"), dict) else {}
        max_age = float(ab.get("playing_game_max_age_sec") or 1800)
        saved = load_playing_game(max_age_sec=max_age)
        if saved and saved.get("game_key"):
            return str(saved["game_key"])
        sc = cfg.get("startup_checks") if isinstance(cfg.get("startup_checks"), dict) else {}
        tk = str(sc.get("token_game_key") or "").strip()
        if tk:
            return tk
    except Exception:
        pass
    return "taixiu_dai_loc"


def save_playing_game(
    *,
    game_id: int,
    game_key: str,
    game_name: str,
    money_vnd: float,
) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "game_id": int(game_id),
        "game_key": str(game_key),
        "game_name": str(game_name),
        "money_vnd": float(money_vnd),
        "updated_at": time.time(),
    }
    with _LOCK:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
