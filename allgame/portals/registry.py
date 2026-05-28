# -*- coding: utf-8 -*-
"""Đăng ký & tra cứu bundle theo portal_id."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from allgame.portals.base import PortalBundle

_REGISTRY: dict[str, PortalBundle] = {}
_BOOTSTRAPPED = False


def register_portal(bundle: PortalBundle) -> None:
    pid = str(bundle.portal_id or "").strip().lower()
    if not pid:
        raise ValueError("portal_id rỗng")
    _REGISTRY[pid] = bundle


def get_portal_bundle(portal_id: str) -> PortalBundle | None:
    bootstrap_portals()
    return _REGISTRY.get(str(portal_id or "").strip().lower())


def list_registered_portal_ids() -> list[str]:
    bootstrap_portals()
    return sorted(_REGISTRY.keys())


def bootstrap_portals() -> None:
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    from allgame.portals.c168 import C168_BUNDLE
    from allgame.portals.cm88 import CM88_BUNDLE
    from allgame.portals.f168 import F168_BUNDLE
    from allgame.portals.fly88 import FLY88_BUNDLE

    for bundle in (CM88_BUNDLE, FLY88_BUNDLE, C168_BUNDLE, F168_BUNDLE):
        register_portal(bundle)
    _BOOTSTRAPPED = True
