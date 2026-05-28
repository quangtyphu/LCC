# -*- coding: utf-8 -*-
"""FLY88 — rút tiền. TODO."""

from __future__ import annotations

from typing import Any

PORTAL_ID = "fly88"


class Fly88Withdrawer:
    portal_id = PORTAL_ID

    def withdraw(self, account: dict[str, Any], amount_vnd: int, **kwargs: Any) -> dict[str, Any]:
        return {"ok": False, "error": "not_implemented", "portal_id": self.portal_id, "amount_vnd": amount_vnd}
