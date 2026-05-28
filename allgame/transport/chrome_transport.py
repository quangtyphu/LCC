# -*- coding: utf-8 -*-
"""
Chrome transport — skeleton.

Luồng đầy đủ (triển khai sau):
  portal.token.test_token → vendor.open_game → vendor.enter_table
"""

from __future__ import annotations

from typing import Any

from allgame.db.accounts_db import account_session_key
from allgame.db.constants import TRANSPORT_CHROME, VENDOR_IDLE
from allgame.orchestrator.session_registry import ActiveSession, SessionRegistry
from allgame.portals.registry import get_portal_bundle


class ChromeTransport:
    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        self.cfg = cfg or {}

    def connect(
        self,
        account: dict[str, Any],
        *,
        registry: SessionRegistry,
    ) -> dict[str, Any]:
        portal_id = str(account.get("portal_id") or "").strip().lower()
        username = str(account.get("username") or "").strip()
        key = account_session_key(portal_id, username)

        bundle = get_portal_bundle(portal_id)
        if not bundle:
            return {"ok": False, "error": "unknown_portal", "session_key": key}

        if portal_id == "c168":
            try:
                from allgame.portals.c168.open_chrome_token import read_and_update_token
                from allgame.portals.c168.check_balance import check_balance
            except Exception as e:
                return {
                    "ok": False,
                    "error": f"import_c168_check_balance_failed: {e}",
                    "session_key": key,
                }
            token_out = read_and_update_token(username)
            if not token_out.get("ok"):
                return {
                    "ok": False,
                    "error": "c168_open_chrome_or_token_failed",
                    "session_key": key,
                    "detail": token_out,
                }
            bal = check_balance(username)
            if not bal.get("ok"):
                return {
                    "ok": False,
                    "error": "c168_balance_check_failed",
                    "session_key": key,
                    "detail": bal,
                }
            session = ActiveSession(
                session_key=key,
                portal_id=portal_id,
                username=username,
                transport=TRANSPORT_CHROME,
                state="connected",
                meta={
                    "balance": bal.get("balance"),
                    "status": bal.get("status"),
                    "code": bal.get("code"),
                    "source": bal.get("source"),
                    "chrome": token_out.get("chrome"),
                    "token_snapshot": token_out.get("token_snapshot"),
                },
            )
            registry.upsert(session)
            return {"ok": True, "session_key": key, "balance": bal.get("balance")}

        # TODO: ensure_chrome → test_token → vendor (other portals)
        session = ActiveSession(
            session_key=key,
            portal_id=portal_id,
            username=username,
            transport=TRANSPORT_CHROME,
            state="stub_connected",
            meta={"note": "skeleton — chưa mở Chrome thật"},
        )
        registry.upsert(session)
        return {"ok": True, "session_key": key, "stub": True}

    def disconnect(
        self,
        session_key: str,
        *,
        registry: SessionRegistry,
    ) -> dict[str, Any]:
        removed = registry.remove(session_key)
        # TODO: kill Chrome / đóng tab vendor
        return {"ok": True, "session_key": session_key, "was_active": removed is not None}

    def health_check(self, session_key: str, *, registry: SessionRegistry) -> bool:
        return registry.get(session_key) is not None
