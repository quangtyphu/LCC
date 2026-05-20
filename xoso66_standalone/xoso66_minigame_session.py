# -*- coding: utf-8 -*-
"""
Session mini-game — đọc token từ DB, refresh khi hết hạn / lỗi.

  from xoso66_minigame_session import ensure_minigame_session

  session = ensure_minigame_session("acc1")
  mg = session["minigame"]
"""

from __future__ import annotations

from typing import Any

from xoso66_minigame_http import get_minigame
from xoso66_minigame_refresh import (
    _is_auth_error,
    _ws_token_age_ok,
    ensure_minigame_tokens,
    ensure_user_token_for_bet,
    fetch_ws_token,
    ping_user_token,
    refresh_minigame_tokens,
    user_token_status,
)


def ensure_minigame_session(
    account_id: str,
    *,
    game_key: str = "taixiu_dai_loc",
    force: bool = False,
) -> dict[str, Any]:
    """Site chính + minigame tokens (lưu DB)."""
    from xoso66_session import ensure_session

    session = ensure_session(account_id, force_login=False)
    ensure_minigame_tokens(
        session,
        account_id=account_id,
        game_key=game_key,
        force=force,
    )
    return session


def refresh_minigame_on_auth_error(
    session: dict,
    account_id: str,
    js: dict[str, Any],
    *,
    game_key: str = "taixiu_dai_loc",
) -> bool:
    """Nếu API mini-game báo lỗi token → gọi refresh; trả True nếu đã refresh."""
    if not _is_auth_error(js):
        return False
    refresh_minigame_tokens(
        session,
        account_id=account_id,
        game_key=game_key,
        force=True,
    )
    return True


def _format_ws_fetch_error(rep: dict[str, Any]) -> str:
    if rep.get("ok"):
        return ""
    parts = []
    if rep.get("error"):
        parts.append(str(rep["error"]))
    if rep.get("msg"):
        parts.append(str(rep["msg"]))
    if rep.get("code") is not None:
        parts.append(f"code={rep['code']}")
    if rep.get("http_status"):
        parts.append(f"http={rep['http_status']}")
    return " | ".join(parts) or str(rep)


def get_ws_token(
    session: dict,
    account_id: str,
    *,
    game_key: str = "taixiu_dai_loc",
    force_refresh: bool = False,
) -> str:
    """Trả ws token; refresh getToken nếu cũ hoặc force."""
    from xoso66_minigame_catalog import game_by_key
    from xoso66_session import ensure_session, persist_session
    from xoso66_sessions_io import apply_session_merge

    mg = get_minigame(session)
    if not mg.get("user_token"):
        apply_session_merge(session, ensure_session(account_id))
        mg = get_minigame(session)

    g = game_by_key(game_key)
    gamename = str(mg.get("gamename") or g.get("gamename") or "lobby")
    rep: dict[str, Any] = {}

    if force_refresh or not _ws_token_age_ok(mg):
        rep = fetch_ws_token(
            session,
            game_id=int(g["game_id"]),
            gamename=gamename,
        )
        if rep.get("ok"):
            persist_session(account_id, session)
        elif not mg.get("ws_token"):
            apply_session_merge(session, ensure_session(account_id))
            mg = get_minigame(session)
            if mg.get("ws_token") and not force_refresh and _ws_token_age_ok(mg):
                return str(mg["ws_token"])
            rep = fetch_ws_token(
                session,
                game_id=int(g["game_id"]),
                gamename=gamename,
            )
            if rep.get("ok"):
                persist_session(account_id, session)

    mg = get_minigame(session)
    token = mg.get("ws_token")
    if not token:
        err = _format_ws_fetch_error(rep)
        hint = f"chạy: python xoso66_minigame_refresh.py -a {account_id} --force"
        raise RuntimeError(
            f"không có ws_token sau refresh{f' ({err})' if err else ''} — {hint}"
        )
    return str(token)
