# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parent
DATA_DIR = _DIR / "data"
PROFILES_DIR = DATA_DIR / "profiles"
REGISTRY_PATH = DATA_DIR / "sessions.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_registry() -> dict[str, Any]:
    if not REGISTRY_PATH.is_file():
        return {"sessions": []}
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def _save_registry(data: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def new_session_id() -> str:
    return secrets.token_hex(4)


def profile_dir(session_id: str) -> Path:
    return PROFILES_DIR / session_id


def register_session(
    *,
    session_id: str,
    proxy: str,
    ephemeral: bool,
    note: str = "",
) -> dict[str, Any]:
    reg = _load_registry()
    row = {
        "id": session_id,
        "created": _now_iso(),
        "proxy": proxy,
        "ephemeral": ephemeral,
        "note": note,
        "profile_dir": str(profile_dir(session_id)),
    }
    reg["sessions"] = [s for s in reg.get("sessions", []) if s.get("id") != session_id]
    reg["sessions"].insert(0, row)
    _save_registry(reg)
    return row


def list_sessions() -> list[dict[str, Any]]:
    return list(_load_registry().get("sessions", []))


def get_session(session_id: str) -> dict[str, Any] | None:
    for s in list_sessions():
        if s.get("id") == session_id:
            return s
    return None


def delete_session(session_id: str) -> bool:
    reg = _load_registry()
    before = len(reg.get("sessions", []))
    reg["sessions"] = [s for s in reg.get("sessions", []) if s.get("id") != session_id]
    if len(reg["sessions"]) == before:
        return False
    _save_registry(reg)
    pdir = profile_dir(session_id)
    if pdir.is_dir():
        shutil.rmtree(pdir, ignore_errors=True)
    return True


def wipe_profile(session_id: str) -> None:
    pdir = profile_dir(session_id)
    if pdir.is_dir():
        shutil.rmtree(pdir, ignore_errors=True)
