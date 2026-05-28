# -*- coding: utf-8 -*-
from allgame.portals.base import PortalBundle
from allgame.portals.c168.deposit import C168Depositor
from allgame.portals.c168.token import C168TokenChecker
from allgame.portals.c168.withdraw import C168Withdrawer

C168_BUNDLE = PortalBundle(
    portal_id="c168",
    token=C168TokenChecker(),
    deposit=C168Depositor(),
    withdraw=C168Withdrawer(),
)

__all__ = ["C168_BUNDLE", "C168Depositor", "C168TokenChecker", "C168Withdrawer"]
