# -*- coding: utf-8 -*-
"""
Một vòng reconcile DB ↔ session RAM (mỗi reconcile_interval_sec).

- Đóng session: không còn «Đang Chơi», đủ cap ngày, đổi nick ưu tiên.
- Mở session: user mới «Đang Chơi», bù slot thiếu.
"""

from __future__ import annotations

from typing import Any

from allgame.config_util import load_config
from allgame.db.accounts_db import (
    account_session_key,
    daily_bet_today_vnd,
    list_playing_accounts,
)
from allgame.orchestrator.session_registry import SessionRegistry, get_registry
from allgame.orchestrator.slot_picker import pick_accounts_to_open
from allgame.transport.chrome_transport import ChromeTransport


def _daily_cap_vnd(cfg: dict[str, Any]) -> float:
    return float(cfg.get("daily_bet_cap_vnd") or 890_000)


def reconcile_once(*, registry: SessionRegistry | None = None) -> dict[str, Any]:
    cfg = load_config()
    reg = registry or get_registry()
    transport = ChromeTransport(cfg)
    cap = _daily_cap_vnd(cfg)
    max_slots = int(cfg.get("chrome_max_concurrent") or 10)

    playing = list_playing_accounts()
    target_keys = {
        account_session_key(str(a["portal_id"]), str(a["username"])) for a in playing
    }
    current_keys = reg.keys()

    closed: list[str] = []
    opened: list[str] = []
    skipped: list[str] = []
    skipped_details: list[dict[str, Any]] = []

    # 1) Đóng session thừa / hết điều kiện
    for key in list(current_keys):
        portal_id, username = SessionRegistry.account_from_key(key)
        acc = next(
            (
                a
                for a in playing
                if str(a.get("portal_id")) == portal_id
                and str(a.get("username")).lower() == username.lower()
            ),
            None,
        )
        should_close = key not in target_keys
        if acc and daily_bet_today_vnd(acc) >= cap:
            should_close = True
        if not should_close:
            continue
        transport.disconnect(key, registry=reg)
        closed.append(key)

    # 2) Mở session thiếu (trong giới hạn slot)
    active_count = len(reg.keys())
    need = max(0, min(max_slots, len(target_keys)) - active_count)
    if need > 0:
        candidates = pick_accounts_to_open(
            playing,
            registry=reg,
            limit=need,
            cfg=cfg,
        )
        for acc in candidates:
            key = account_session_key(str(acc["portal_id"]), str(acc["username"]))
            if key in reg.keys():
                skipped.append(key)
                continue
            if len(reg.keys()) >= max_slots:
                break
            result = transport.connect(acc, registry=reg)
            if result.get("ok"):
                opened.append(key)
            else:
                skipped.append(key)
                skipped_details.append(
                    {
                        "session_key": key,
                        "error": result.get("error"),
                        "detail": result.get("detail"),
                    }
                )

    return {
        "ok": True,
        "target_count": len(target_keys),
        "active_count": len(reg.keys()),
        "closed": closed,
        "opened": opened,
        "skipped": skipped,
        "skipped_details": skipped_details,
    }
