# -*- coding: utf-8 -*-
"""Giao diện chung — mỗi cổng game implement token / deposit / withdraw riêng."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PortalTokenChecker(Protocol):
    """Đọc & kiểm tra session/token cổng A (lobby)."""

    portal_id: str

    def test_token(self, account: dict[str, Any]) -> bool:
        """True nếu token/session còn sống."""
        ...

    def read_token_snapshot(self, account: dict[str, Any]) -> dict[str, Any]:
        """Metadata token (không trả password). Dùng log / CMS."""
        ...

    def refresh_token(self, account: dict[str, Any]) -> dict[str, Any]:
        """
        Làm mới session; trả dict cập nhật session_json hoặc
        {'ok': False, 'error': '...'}.
        """
        ...


@runtime_checkable
class PortalDepositor(Protocol):
    """Nạp tiền — logic riêng từng cổng."""

    portal_id: str

    def deposit(
        self,
        account: dict[str, Any],
        amount_vnd: int,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Trả {'ok': bool, ...}."""
        ...


@runtime_checkable
class PortalWithdrawer(Protocol):
    """Rút tiền — logic riêng từng cổng."""

    portal_id: str

    def withdraw(
        self,
        account: dict[str, Any],
        amount_vnd: int,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Trả {'ok': bool, ...}."""
        ...


@dataclass(frozen=True)
class PortalBundle:
    """Gom 3 module của một cổng game."""

    portal_id: str
    token: PortalTokenChecker
    deposit: PortalDepositor
    withdraw: PortalWithdrawer
