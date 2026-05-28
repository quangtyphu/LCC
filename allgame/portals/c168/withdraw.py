# -*- coding: utf-8 -*-
"""C168 — rút tiền. TODO: nối logic rút từ c168_standalone / CMS."""

from __future__ import annotations

from typing import Any

PORTAL_ID = "c168"


class C168Withdrawer:
    portal_id = PORTAL_ID

    def withdraw(
        self,
        account: dict[str, Any],
        amount_vnd: int,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "error": "not_implemented",
            "portal_id": self.portal_id,
            "username": account.get("username"),
            "amount_vnd": int(amount_vnd),
        }
