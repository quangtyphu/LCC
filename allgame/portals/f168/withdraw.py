# -*- coding: utf-8 -*-
"""F168 — rút tiền. TODO."""

from __future__ import annotations

from typing import Any

PORTAL_ID = "f168"


class F168Withdrawer:
    portal_id = PORTAL_ID

    def withdraw(self, account: dict[str, Any], amount_vnd: int, **kwargs: Any) -> dict[str, Any]:
        return {"ok": False, "error": "not_implemented", "portal_id": self.portal_id, "amount_vnd": amount_vnd}
