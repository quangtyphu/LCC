# -*- coding: utf-8 -*-
"""Gửi cảnh báo auto-bet qua Telegram (config hoặc telegram_notifier LC79)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import requests

_DIR = Path(__file__).resolve().parent
_LC79_ROOT = _DIR.parent


def _load_cfg(cfg: dict | None) -> dict:
    if cfg is None:
        from xoso66_config_util import load_config

        return load_config()
    return cfg


def _auto_bet_cfg(cfg: dict | None) -> dict:
    raw = _load_cfg(cfg).get("auto_bet")
    return raw if isinstance(raw, dict) else {}


def _auto_mission_cfg(cfg: dict | None) -> dict:
    raw = _load_cfg(cfg).get("auto_mission_reward")
    return raw if isinstance(raw, dict) else {}


def _telegram_credentials(cfg: dict | None, *, section: dict) -> tuple[bool, str, str]:
    """enabled, token, chat_id — section trước, rồi auto_bet, rồi env."""
    ab = _auto_bet_cfg(cfg)
    enabled = section.get("telegram_enabled")
    if enabled is None:
        enabled = ab.get("telegram_enabled", True)
    if enabled is False:
        return False, "", ""
    token = str(
        section.get("telegram_bot_token")
        or ab.get("telegram_bot_token")
        or os.environ.get("XOSO66_TELEGRAM_TOKEN")
        or ""
    ).strip()
    chat = str(
        section.get("telegram_chat_id")
        or ab.get("telegram_chat_id")
        or os.environ.get("XOSO66_TELEGRAM_CHAT_ID")
        or ""
    ).strip()
    return bool(enabled), token, chat


def _send_via_api(token: str, chat_id: str, msg: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": chat_id, "text": msg}, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"[TELEGRAM] Gửi lỗi: {e}", flush=True)
        return False


def _notify_telegram(
    msg: str,
    *,
    cfg: dict | None,
    section: dict,
    prefix: str = "XOSO66",
) -> bool:
    enabled, token, chat = _telegram_credentials(cfg, section=section)
    if not enabled:
        return False
    text = f"[{prefix}]\n{msg}".strip()
    if token and chat:
        return _send_via_api(token, chat, text)
    if str(_LC79_ROOT) not in sys.path:
        sys.path.insert(0, str(_LC79_ROOT))
    try:
        from telegram_notifier import send_telegram

        return bool(send_telegram(text))
    except Exception as e:
        print(f"[TELEGRAM] Không gửi được (thiếu config / import): {e}", flush=True)
        return False


def notify_auto_bet(msg: str, *, cfg: dict | None = None, prefix: str = "XOSO66") -> bool:
    return _notify_telegram(msg, cfg=cfg, section=_auto_bet_cfg(cfg), prefix=prefix)


def notify_auto_mission(msg: str, *, cfg: dict | None = None, prefix: str = "XOSO66 MISSION") -> bool:
    return _notify_telegram(msg, cfg=cfg, section=_auto_mission_cfg(cfg), prefix=prefix)
