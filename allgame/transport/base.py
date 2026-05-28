# -*- coding: utf-8 -*-
"""Transport — kết nối vật lý tới game (Chrome / WS)."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from allgame.orchestrator.session_registry import SessionRegistry


@runtime_checkable
class GameTransport(Protocol):
    def connect(
        self,
        account: dict[str, Any],
        *,
        registry: SessionRegistry,
    ) -> dict[str, Any]:
        ...

    def disconnect(
        self,
        session_key: str,
        *,
        registry: SessionRegistry,
    ) -> dict[str, Any]:
        ...

    def health_check(self, session_key: str, *, registry: SessionRegistry) -> bool:
        ...
