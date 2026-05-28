# -*- coding: utf-8 -*-
"""Đường dẫn chuẩn — DB/config XOSO66 nằm trên CMS (game_data/), không dùng xoso66_standalone/data/."""

from __future__ import annotations

import os
from pathlib import Path

_DIR = Path(__file__).resolve().parent


def cms_root() -> Path:
    explicit = (os.environ.get("XOSO66_CMS_ROOT") or "").strip()
    if explicit:
        return Path(explicit).resolve()
    return (_DIR.parent.parent / "CMS").resolve()


def cms_game_data_dir() -> Path:
    explicit = (os.environ.get("XOSO66_DATA_DIR") or "").strip()
    if explicit:
        return Path(explicit).resolve()
    return (cms_root() / "game_data").resolve()


def default_db_path() -> Path:
    explicit = (os.environ.get("XOSO66_DB") or "").strip()
    if explicit:
        return Path(explicit).resolve()
    return cms_game_data_dir() / "xoso66.db"


def default_config_path() -> Path:
    explicit = (os.environ.get("XOSO66_CONFIG") or "").strip()
    if explicit:
        return Path(explicit).resolve()
    cms_cfg = cms_game_data_dir() / "xoso66_config.json"
    if cms_cfg.is_file():
        return cms_cfg
    return _DIR / "xoso66_config.json"


def apply_default_env() -> None:
    """Gọi sớm (main.py) trước import DB — mọi worker dùng CMS/game_data."""
    gd = cms_game_data_dir()
    os.environ.setdefault("XOSO66_DATA_DIR", str(gd))
    os.environ.setdefault("XOSO66_DB", str(gd / "xoso66.db"))
    cfg = default_config_path()
    os.environ.setdefault("XOSO66_CONFIG", str(cfg))
