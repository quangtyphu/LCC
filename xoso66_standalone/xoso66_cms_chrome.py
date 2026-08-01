# -*- coding: utf-8 -*-
"""Đọc profile Chrome từ CMS (game_data.db → chrome_profiles)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from xoso66_paths import cms_root as _cms_root

_CMS_ROOT = Path(os.environ.get("CMS_ROOT") or os.environ.get("XOSO66_CMS_ROOT") or "").resolve() if (
    os.environ.get("CMS_ROOT") or os.environ.get("XOSO66_CMS_ROOT")
) else _cms_root()
_GAME_DATA_DB = Path(
    os.environ.get("GAME_DATA_DB")
    or os.environ.get("CMS_GAME_DATA_DB")
    or _CMS_ROOT / "game_data.db"
)
_PROFILES_DIR = Path(
    os.environ.get("CMS_CHROME_PROFILES_DIR") or _CMS_ROOT / "chrome_profiles_data"
)


def resolve_cms_chrome_by_device(device: str) -> dict[str, Any] | None:
    """Tìm profile Chrome CMS theo tên thiết bị (vd. XMSB17)."""
    dev = str(device or "").strip()
    if not dev or not _GAME_DATA_DB.is_file():
        return None
    conn = sqlite3.connect(_GAME_DATA_DB)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM chrome_profiles WHERE TRIM(device) = ? COLLATE NOCASE LIMIT 1",
            (dev,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    d = dict(row)
    sid = str(d.get("session_id") or "").strip()
    profile_dir = _PROFILES_DIR / sid if sid else None
    return {
        "device": str(d.get("device") or dev),
        "name": str(d.get("name") or ""),
        "proxy": str(d.get("proxy") or "").strip(),
        "session_id": sid,
        "profile_dir": str(profile_dir) if profile_dir else "",
        "phone": str(d.get("phone") or ""),
        "bank": str(d.get("bank") or ""),
        "account_number": str(d.get("account_number") or ""),
        "account_holder": str(d.get("account_holder") or ""),
    }


def _proxy_key(proxy_str: str) -> str:
    from xoso66_proxy import parse_proxy

    try:
        host, port, user, pwd = parse_proxy(str(proxy_str or "").strip())
        return f"{host.lower()}:{port}:{user}:{pwd}"
    except ValueError:
        return str(proxy_str or "").strip().lower()


def device_proxy_mismatch(row: dict[str, Any]) -> dict[str, Any] | None:
    """
    So sánh proxy account vs proxy Chrome CMS (theo cột device).
    cf_clearance gắn IP/proxy — khác proxy thì cookie Chrome không dùng được cho API account.
    """
    dev = str(row.get("device") or "").strip()
    if not dev:
        return None
    cms = resolve_cms_chrome_by_device(dev)
    if not cms:
        return None
    acc_px = str(row.get("proxy") or "").strip()
    cms_px = str(cms.get("proxy") or "").strip()
    if not acc_px or not cms_px:
        return None
    if _proxy_key(acc_px) == _proxy_key(cms_px):
        return None
    return {
        "device": dev,
        "account_proxy": acc_px,
        "device_proxy": cms_px,
        "message": (
            f"Proxy account khác Chrome {dev}. "
            "Sync sẽ dùng proxy của Chrome CMS (cf_clearance gắn IP đó)."
        ),
    }
