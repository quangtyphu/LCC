# -*- coding: utf-8 -*-
"""Bảng đăng ký cổng game (portal) — domain, transport, bật/tắt."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from allgame.db.accounts_db import db_conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

_PORTALS_DDL = """
CREATE TABLE IF NOT EXISTS portals (
    portal_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    site_url TEXT NOT NULL DEFAULT '',
    transport TEXT NOT NULL DEFAULT 'chrome',
    enabled INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

DEFAULT_PORTALS: tuple[dict[str, Any], ...] = (
    {
        "portal_id": "cm88",
        "display_name": "CM88",
        "site_url": "https://cm88.com/",
        "transport": "chrome",
        "enabled": 1,
        "config_json": {},
    },
    {
        "portal_id": "fly88",
        "display_name": "FLY88",
        "site_url": "https://m.fly88t.vip/",
        "transport": "chrome",
        "enabled": 1,
        "config_json": {},
    },
    {
        "portal_id": "c168",
        "display_name": "C168",
        "site_url": "https://c168b2.cc/",
        "transport": "chrome",
        "enabled": 1,
        "config_json": {"hall_api": "https://af861c.c168f.com", "sitecode": "2865"},
    },
    {
        "portal_id": "f168",
        "display_name": "F168",
        "site_url": "https://f1686s.com/",
        "transport": "chrome",
        "enabled": 1,
        "config_json": {},
    },
    {
        "portal_id": "sc88",
        "display_name": "SC88",
        "site_url": "https://www.sc881.net/",
        "transport": "chrome",
        "enabled": 1,
        "config_json": {},
    },
)


def init_portals(seed_defaults: bool = True) -> None:
    with db_conn() as conn:
        conn.execute(_PORTALS_DDL)
        if not seed_defaults:
            return
        for p in DEFAULT_PORTALS:
            now = _now_iso()
            cfg = p.get("config_json")
            cfg_s = json.dumps(cfg, ensure_ascii=False) if isinstance(cfg, dict) else "{}"
            conn.execute(
                """
                INSERT INTO portals (
                    portal_id, display_name, site_url, transport, enabled,
                    config_json, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(portal_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    site_url = excluded.site_url,
                    transport = excluded.transport,
                    enabled = excluded.enabled,
                    config_json = excluded.config_json,
                    updated_at = excluded.updated_at
                """,
                (
                    p["portal_id"],
                    p.get("display_name") or p["portal_id"],
                    p.get("site_url") or "",
                    p.get("transport") or "chrome",
                    int(p.get("enabled", 1)),
                    cfg_s,
                    now,
                    now,
                ),
            )
        active_ids = [p["portal_id"] for p in DEFAULT_PORTALS]
        placeholders = ",".join("?" for _ in active_ids)
        conn.execute(
            f"UPDATE portals SET enabled = 0 WHERE portal_id NOT IN ({placeholders})",
            active_ids,
        )


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    raw = d.get("config_json") or "{}"
    if isinstance(raw, str):
        try:
            d["config_json"] = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            d["config_json"] = {}
    d["enabled"] = bool(d.get("enabled"))
    return d


def get_portal(portal_id: str) -> dict[str, Any] | None:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM portals WHERE portal_id = ?",
            (str(portal_id).strip().lower(),),
        ).fetchone()
    return _row_to_dict(row)


def list_portals(*, enabled_only: bool = False) -> list[dict[str, Any]]:
    with db_conn() as conn:
        if enabled_only:
            rows = conn.execute(
                "SELECT * FROM portals WHERE enabled = 1 ORDER BY portal_id"
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM portals ORDER BY portal_id").fetchall()
    return [_row_to_dict(r) or {} for r in rows]


def upsert_portal(fields: dict[str, Any]) -> dict[str, Any]:
    portal_id = str(fields.get("portal_id") or "").strip().lower()
    if not portal_id:
        raise ValueError("Thiếu portal_id")

    existing = get_portal(portal_id)
    now = _now_iso()
    created = (existing or {}).get("created_at") or now

    cfg = fields.get("config_json")
    if isinstance(cfg, dict):
        cfg_s = json.dumps(cfg, ensure_ascii=False)
    elif cfg is None:
        cfg_s = json.dumps((existing or {}).get("config_json") or {}, ensure_ascii=False)
    else:
        cfg_s = str(cfg)

    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO portals (
                portal_id, display_name, site_url, transport, enabled,
                config_json, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(portal_id) DO UPDATE SET
                display_name=excluded.display_name,
                site_url=excluded.site_url,
                transport=excluded.transport,
                enabled=excluded.enabled,
                config_json=excluded.config_json,
                updated_at=excluded.updated_at
            """,
            (
                portal_id,
                str(fields.get("display_name", (existing or {}).get("display_name", ""))),
                str(fields.get("site_url", (existing or {}).get("site_url", ""))),
                str(fields.get("transport", (existing or {}).get("transport", "chrome"))),
                int(fields.get("enabled", (existing or {}).get("enabled", 1))),
                cfg_s,
                created,
                now,
            ),
        )
    return get_portal(portal_id) or {"portal_id": portal_id}
