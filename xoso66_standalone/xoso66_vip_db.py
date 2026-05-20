# -*- coding: utf-8 -*-
"""
SQLite — snapshot VIP từ /server/activity/vipList (sync sau xoso66_vip_check).

Lưu cấp VIP hiện tại + 3 loại thưởng tại cấp đó (upgrade / weekly / monthly).
prize status: 0 chưa đạt, 1 được nhận, 2 đã nhận, 3 chờ mốc tuần/tháng.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from xoso66_accounts_db import DB_PATH, db_conn, init_db, update_account

VIP_STATUS_LABEL = {
    0: "Chưa đạt",
    1: "Được nhận",
    2: "Đã nhận",
    3: "Chờ mốc",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_VIP_DDL = """
CREATE TABLE IF NOT EXISTS account_vip (
    username TEXT PRIMARY KEY COLLATE NOCASE,
    account_id TEXT NOT NULL DEFAULT '',
    activity_id INTEGER NOT NULL DEFAULT 1,
    activity_title TEXT NOT NULL DEFAULT '',
    current_level INTEGER NOT NULL DEFAULT 0,
    vip_level TEXT NOT NULL DEFAULT '',
    vip_progress INTEGER NOT NULL DEFAULT 0,
    next_level INTEGER,
    completed_value INTEGER NOT NULL DEFAULT 0,
    target_value INTEGER NOT NULL DEFAULT 0,
    reward_count INTEGER NOT NULL DEFAULT 0,
    level_id INTEGER NOT NULL DEFAULT 0,
    upgrade_status INTEGER,
    upgrade_prize INTEGER NOT NULL DEFAULT 0,
    weekly_status INTEGER,
    weekly_prize INTEGER NOT NULL DEFAULT 0,
    monthly_status INTEGER,
    monthly_prize INTEGER NOT NULL DEFAULT 0,
    can_claim_count INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL,
    sync_phase TEXT NOT NULL DEFAULT 'list'
)
"""


def init_vip_table(conn: sqlite3.Connection | None = None) -> None:
    if conn is not None:
        conn.execute(_VIP_DDL)
        return
    with db_conn() as c:
        init_vip_table(c)


def _find_level_row(data: dict[str, Any], current_level: int) -> dict[str, Any] | None:
    levels = data.get("level_list") or data.get("levelList") or []
    if not isinstance(levels, list):
        return None
    for lv in levels:
        if isinstance(lv, dict) and int(lv.get("level") or 0) == current_level:
            return lv
    return None


def _prize_fields(level_row: dict[str, Any] | None, reward_type: str) -> tuple[int | None, int]:
    if not level_row:
        return None, 0
    rt = reward_type.strip().lower()
    for p in level_row.get("prize_list") or []:
        if not isinstance(p, dict):
            continue
        if str(p.get("reward_type") or "").strip().lower() == rt:
            st = p.get("status")
            try:
                status = int(st) if st is not None else None
            except (TypeError, ValueError):
                status = None
            return status, int(p.get("prize") or 0)
    return None, 0


def _status_label(status: int | None) -> str:
    if status is None:
        return "—"
    return VIP_STATUS_LABEL.get(int(status), f"?({status})")


def build_vip_snapshot(
    username: str,
    account_id: str,
    data: dict[str, Any] | None,
    *,
    phase: str = "list",
) -> dict[str, Any]:
    """Từ vipList.data → dict lưu account_vip."""
    u = str(username or "").strip()
    aid = str(account_id or "").strip()
    if not isinstance(data, dict):
        data = {}
    vd = data.get("vip_data") or data.get("vipData") or {}
    if not isinstance(vd, dict):
        vd = {}
    try:
        current = int(vd.get("current_level") or 0)
    except (TypeError, ValueError):
        current = 0
    try:
        progress = max(0, min(100, int(vd.get("progress") or 0)))
    except (TypeError, ValueError):
        progress = 0
    lv_row = _find_level_row(data, current) if current else None
    level_label = f"VIP{current}" if current else ""
    level_id = 0
    if lv_row:
        try:
            level_id = int(
                lv_row.get("id") if lv_row.get("id") is not None else lv_row.get("level") or current
            )
        except (TypeError, ValueError):
            level_id = current
        level_label = str(lv_row.get("level_formatted") or level_label)
    up_st, up_prize = _prize_fields(lv_row, "upgrade")
    wk_st, wk_prize = _prize_fields(lv_row, "weekly")
    mo_st, mo_prize = _prize_fields(lv_row, "monthly")
    can_claim = 0
    if lv_row:
        for p in lv_row.get("prize_list") or []:
            if isinstance(p, dict) and int(p.get("status") or 0) == 1:
                can_claim += 1
    try:
        activity_id = int(data.get("id") or data.get("activity_id") or 1)
    except (TypeError, ValueError):
        activity_id = 1
    try:
        next_level = int(vd["next_level"]) if vd.get("next_level") is not None else None
    except (TypeError, ValueError):
        next_level = None
    return {
        "username": u,
        "account_id": aid,
        "activity_id": activity_id,
        "activity_title": str(data.get("title") or ""),
        "current_level": current,
        "vip_level": level_label,
        "vip_progress": progress,
        "next_level": next_level,
        "completed_value": int(vd.get("completed_value") or 0),
        "target_value": int(vd.get("target_value") or 0),
        "reward_count": int(vd.get("reward_count") or 0),
        "level_id": level_id,
        "upgrade_status": up_st,
        "upgrade_prize": up_prize,
        "weekly_status": wk_st,
        "weekly_prize": wk_prize,
        "monthly_status": mo_st,
        "monthly_prize": mo_prize,
        "can_claim_count": can_claim,
        "synced_at": _now_iso(),
        "sync_phase": str(phase or "list"),
        "upgrade_label": _status_label(up_st),
        "weekly_label": _status_label(wk_st),
        "monthly_label": _status_label(mo_st),
    }


def upsert_vip_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
    init_db()
    with db_conn() as conn:
        init_vip_table(conn)
        conn.execute(
            """
            INSERT INTO account_vip (
                username, account_id, activity_id, activity_title,
                current_level, vip_level, vip_progress, next_level,
                completed_value, target_value, reward_count, level_id,
                upgrade_status, upgrade_prize,
                weekly_status, weekly_prize,
                monthly_status, monthly_prize,
                can_claim_count, synced_at, sync_phase
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(username) DO UPDATE SET
                account_id=excluded.account_id,
                activity_id=excluded.activity_id,
                activity_title=excluded.activity_title,
                current_level=excluded.current_level,
                vip_level=excluded.vip_level,
                vip_progress=excluded.vip_progress,
                next_level=excluded.next_level,
                completed_value=excluded.completed_value,
                target_value=excluded.target_value,
                reward_count=excluded.reward_count,
                level_id=excluded.level_id,
                upgrade_status=excluded.upgrade_status,
                upgrade_prize=excluded.upgrade_prize,
                weekly_status=excluded.weekly_status,
                weekly_prize=excluded.weekly_prize,
                monthly_status=excluded.monthly_status,
                monthly_prize=excluded.monthly_prize,
                can_claim_count=excluded.can_claim_count,
                synced_at=excluded.synced_at,
                sync_phase=excluded.sync_phase
            """,
            (
                snap["username"],
                snap["account_id"],
                snap["activity_id"],
                snap["activity_title"],
                snap["current_level"],
                snap["vip_level"],
                snap["vip_progress"],
                snap["next_level"],
                snap["completed_value"],
                snap["target_value"],
                snap["reward_count"],
                snap["level_id"],
                snap["upgrade_status"],
                snap["upgrade_prize"],
                snap["weekly_status"],
                snap["weekly_prize"],
                snap["monthly_status"],
                snap["monthly_prize"],
                snap["can_claim_count"],
                snap["synced_at"],
                snap["sync_phase"],
            ),
        )
    return snap


def persist_vip_state(
    username: str,
    account_id: str,
    data: dict[str, Any] | None,
    *,
    phase: str = "list",
) -> dict[str, Any]:
    """
    Lưu account_vip + đồng bộ accounts.vip_level / vip_progress.
    Gọi sau vipList và sau nhận thưởng.
    """
    snap = build_vip_snapshot(username, account_id, data, phase=phase)
    upsert_vip_snapshot(snap)
    if snap.get("current_level"):
        update_account(
            str(account_id),
            {
                "vip_level": str(snap.get("vip_level") or ""),
                "vip_progress": int(snap.get("vip_progress") or 0),
            },
        )
    snap["db_path"] = str(DB_PATH.resolve())
    snap["save_phase"] = phase
    return snap


def format_db_save_line(snap: dict[str, Any]) -> str:
    phase = snap.get("save_phase") or snap.get("sync_phase") or "list"
    return (
        f"DB [{phase}] {snap.get('db_path')} | account_vip OK | "
        f"{snap.get('vip_level')} {int(snap.get('vip_progress') or 0)}% | "
        f"↑{snap.get('upgrade_label')} {int(snap.get('upgrade_prize') or 0):,} · "
        f"T{snap.get('weekly_label')} {int(snap.get('weekly_prize') or 0):,} · "
        f"Th{snap.get('monthly_label')} {int(snap.get('monthly_prize') or 0):,}"
        + (
            f" | claimable={int(snap.get('can_claim_count') or 0)}"
            if int(snap.get("can_claim_count") or 0)
            else ""
        )
    )


def get_vip_snapshot(username: str) -> dict[str, Any] | None:
    init_db()
    u = str(username or "").strip()
    with db_conn() as conn:
        init_vip_table(conn)
        row = conn.execute(
            "SELECT * FROM account_vip WHERE username = ? COLLATE NOCASE",
            (u,),
        ).fetchone()
    return dict(row) if row else None


def list_vip_snapshots() -> list[dict[str, Any]]:
    init_db()
    with db_conn() as conn:
        init_vip_table(conn)
        rows = conn.execute("SELECT * FROM account_vip ORDER BY username").fetchall()
    return [dict(r) for r in rows]


def vip_index_by_account() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for snap in list_vip_snapshots():
        u = str(snap.get("username") or "").strip().lower()
        aid = str(snap.get("account_id") or "").strip()
        if u:
            out[f"u:{u}"] = snap
        if aid:
            out[f"id:{aid}"] = snap
    return out


def vip_snapshot_for_account(
    account_id: str,
    username: str,
    *,
    index: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    idx = index if index is not None else vip_index_by_account()
    u = str(username or "").strip().lower()
    aid = str(account_id or "").strip()
    if u and f"u:{u}" in idx:
        return idx[f"u:{u}"]
    if aid and f"id:{aid}" in idx:
        return idx[f"id:{aid}"]
    return None


def vip_cms_fields(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Trường gắn vào /api/accounts cho CMS."""
    if not snapshot:
        return {
            "vip_synced_at": None,
            "vip_upgrade_label": "—",
            "vip_weekly_label": "—",
            "vip_monthly_label": "—",
        }
    return {
        "vip_synced_at": snapshot.get("synced_at"),
        "vip_sync_phase": snapshot.get("sync_phase"),
        "vip_can_claim": int(snapshot.get("can_claim_count") or 0),
        "vip_upgrade_status": snapshot.get("upgrade_status"),
        "vip_upgrade_label": _status_label(snapshot.get("upgrade_status")),
        "vip_upgrade_prize": int(snapshot.get("upgrade_prize") or 0),
        "vip_weekly_status": snapshot.get("weekly_status"),
        "vip_weekly_label": _status_label(snapshot.get("weekly_status")),
        "vip_weekly_prize": int(snapshot.get("weekly_prize") or 0),
        "vip_monthly_status": snapshot.get("monthly_status"),
        "vip_monthly_label": _status_label(snapshot.get("monthly_status")),
        "vip_monthly_prize": int(snapshot.get("monthly_prize") or 0),
    }
