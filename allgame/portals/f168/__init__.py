# -*- coding: utf-8 -*-
from allgame.portals.base import PortalBundle
from allgame.portals.f168.deposit import F168Depositor
from allgame.portals.f168.token import F168TokenChecker
from allgame.portals.f168.withdraw import F168Withdrawer

F168_BUNDLE = PortalBundle(
    portal_id="f168",
    token=F168TokenChecker(),
    deposit=F168Depositor(),
    withdraw=F168Withdrawer(),
)

__all__ = ["F168_BUNDLE"]
