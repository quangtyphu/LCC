# -*- coding: utf-8 -*-
from allgame.portals.base import PortalBundle
from allgame.portals.fly88.deposit import Fly88Depositor
from allgame.portals.fly88.token import Fly88TokenChecker
from allgame.portals.fly88.withdraw import Fly88Withdrawer

FLY88_BUNDLE = PortalBundle(
    portal_id="fly88",
    token=Fly88TokenChecker(),
    deposit=Fly88Depositor(),
    withdraw=Fly88Withdrawer(),
)

__all__ = ["FLY88_BUNDLE"]
