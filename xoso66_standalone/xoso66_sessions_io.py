# -*- coding: utf-8 -*-
"""Đọc/ghi session — SQLite (mặc định) hoặc xoso66_sessions.json."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DIR = Path(__file__).resolve().parent
SESSIONS_FILE = Path(os.environ.get("XOSO66_SESSIONS", DIR / "xoso66_sessions.json"))


def use_db() -> bool:
    if os.environ.get("XOSO66_USE_DB", "1").strip() in ("0", "false", "no"):
        return False
    from xoso66_accounts_db import DB_PATH, init_db

    if os.environ.get("XOSO66_USE_DB", "").strip() == "1":
        init_db()
        return True
    if DB_PATH.is_file():
        return True
    if os.environ.get("XOSO66_AUTO_INIT_DB", "1").strip() not in ("0", "false", "no"):
        init_db()
        return True
    return False


def load_sessions() -> dict[str, dict]:
    if use_db():
        from xoso66_accounts_db import load_all_as_sessions

        return load_all_as_sessions()
    if not SESSIONS_FILE.is_file():
        raise FileNotFoundError(f"Chưa có {SESSIONS_FILE.name}")
    data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
    accounts = data.get("accounts") or data
    if isinstance(accounts, list):
        return {str(a["id"]): a for a in accounts if a.get("id")}
    if isinstance(accounts, dict):
        return {str(k): v for k, v in accounts.items()}
    raise ValueError("xoso66_sessions.json: cần 'accounts': [...]")


def save_sessions(accounts: dict[str, dict]) -> None:
    if use_db():
        from xoso66_accounts_db import save_accounts_from_session_map

        save_accounts_from_session_map(accounts)
        return
    ordered = sorted(accounts.values(), key=lambda a: str(a.get("id", "")))
    SESSIONS_FILE.write_text(
        json.dumps({"accounts": ordered}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def apply_session_merge(target: dict, patch: dict) -> None:
    """Gộp patch vào target tại chỗ (dùng sau ensure_session / refresh token)."""
    merged = merge_account(target, patch)
    target.clear()
    target.update(merged)


def merge_account(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        if k == "cookies" and isinstance(v, dict):
            cookies = dict(out.get("cookies") or {})
            cookies.update(v)
            out["cookies"] = cookies
        elif k == "headers" and isinstance(v, dict):
            headers = dict(out.get("headers") or {})
            headers.update(v)
            out["headers"] = headers
        elif k == "minigame" and isinstance(v, dict):
            from xoso66_accounts_db import _merge_minigame_dict

            out["minigame"] = _merge_minigame_dict(
                out.get("minigame") if isinstance(out.get("minigame"), dict) else {},
                v,
            )
        else:
            out[k] = v
    return out
