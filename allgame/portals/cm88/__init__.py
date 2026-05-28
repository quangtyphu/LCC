# -*- coding: utf-8 -*-
from allgame.portals.base import PortalBundle
from allgame.portals.cm88.deposit import Cm88Depositor
from allgame.portals.cm88.token import Cm88TokenChecker
from allgame.portals.cm88.withdraw import Cm88Withdrawer

CM88_BUNDLE = PortalBundle(
    portal_id="cm88",
    token=Cm88TokenChecker(),
    deposit=Cm88Depositor(),
    withdraw=Cm88Withdrawer(),
)

__all__ = ["CM88_BUNDLE"]
