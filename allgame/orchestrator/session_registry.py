# -*- coding: utf-8 -*-
"""Session runtime trong RAM — Chrome / vendor (không lưu DB)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from allgame.db.accounts_db import account_session_key, parse_session_key


@dataclass
class ActiveSession:
    session_key: str
    portal_id: str
    username: str
    transport: str
    state: str = "connecting"
    chrome_cdp_url: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


class SessionRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, ActiveSession] = {}

    def keys(self) -> set[str]:
        with self._lock:
            return set(self._sessions.keys())

    def get(self, session_key: str) -> ActiveSession | None:
        with self._lock:
            return self._sessions.get(session_key)

    def upsert(self, session: ActiveSession) -> None:
        with self._lock:
            self._sessions[session.session_key] = session

    def remove(self, session_key: str) -> ActiveSession | None:
        with self._lock:
            return self._sessions.pop(session_key, None)

    def list_all(self) -> list[ActiveSession]:
        with self._lock:
            return list(self._sessions.values())

    @staticmethod
    def key_for_account(account: dict[str, Any]) -> str:
        return account_session_key(
            str(account.get("portal_id") or ""),
            str(account.get("username") or ""),
        )

    @staticmethod
    def account_from_key(session_key: str) -> tuple[str, str]:
        return parse_session_key(session_key)


_registry = SessionRegistry()


def get_registry() -> SessionRegistry:
    return _registry
