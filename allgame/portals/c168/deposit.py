# -*- coding: utf-8 -*-
"""C168 — nạp tiền. TODO: nối logic nạp từ c168_standalone / CMS."""

from __future__ import annotations

from typing import Any

PORTAL_ID = "c168"


class C168Depositor:
    portal_id = PORTAL_ID

    def deposit(
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
