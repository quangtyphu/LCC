# -*- coding: utf-8 -*-
from allgame.portals.base import PortalBundle
from allgame.portals.benbet.deposit import BenbetDepositor
from allgame.portals.benbet.token import BenbetTokenChecker
from allgame.portals.benbet.withdraw import BenbetWithdrawer

BENBET_BUNDLE = PortalBundle(
    portal_id="benbet",
    token=BenbetTokenChecker(),
    deposit=BenbetDepositor(),
    withdraw=BenbetWithdrawer(),
)

__all__ = ["BENBET_BUNDLE"]
