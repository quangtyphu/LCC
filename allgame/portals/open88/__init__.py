# -*- coding: utf-8 -*-
from allgame.portals.base import PortalBundle
from allgame.portals.open88.deposit import Open88Depositor
from allgame.portals.open88.token import Open88TokenChecker
from allgame.portals.open88.withdraw import Open88Withdrawer

OPEN88_BUNDLE = PortalBundle(
    portal_id="open88",
    token=Open88TokenChecker(),
    deposit=Open88Depositor(),
    withdraw=Open88Withdrawer(),
)

__all__ = ["OPEN88_BUNDLE"]
