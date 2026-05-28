# -*- coding: utf-8 -*-
"""Open88 — token. TODO."""

from __future__ import annotations

from typing import Any

PORTAL_ID = "open88"


class Open88TokenChecker:
    portal_id = PORTAL_ID

    def test_token(self, account: dict[str, Any]) -> bool:
        raise NotImplementedError("Open88 token: triển khai sau.")

    def read_token_snapshot(self, account: dict[str, Any]) -> dict[str, Any]:
        return {"portal_id": self.portal_id, "username": account.get("username"), "implemented": False}

    def refresh_token(self, account: dict[str, Any]) -> dict[str, Any]:
        return {"ok": False, "error": "not_implemented", "portal_id": self.portal_id}
