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
    prep_tokens_before_ws,
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


def _auto_refresh_tokens_for_ws(
    session: dict,
    account_id: str,
    *,
    game_key: str,
    reason: str,
) -> dict[str, Any]:
    """Thiếu/hỏng user-token → tự prep (gameurl/CF/ws) rồi lưu DB."""
    # 401 / token hết hạn: refresh im lặng (tránh spam log).
    if reason != "user-token hết hạn / getToken 401":
        from xoso66_accounts_db import username_for_log

        user = username_for_log(account_id, session)
        print(f"[WS] [{user}] {reason} — tự refresh minigame…", flush=True)
    return prep_tokens_before_ws(
        session,
        account_id,
        game_key=game_key,
        force_ws=True,
    )


def get_ws_token(
    session: dict,
    account_id: str,
    *,
    game_key: str = "taixiu_dai_loc",
    force_refresh: bool = False,
) -> str:
    """Trả ws token; thiếu user-token thì tự refresh; getToken nếu cũ hoặc force."""
    from xoso66_minigame_catalog import game_by_key
    from xoso66_session import ensure_session, persist_session
    from xoso66_sessions_io import apply_session_merge

    mg = get_minigame(session)
    if not mg.get("user_token"):
        apply_session_merge(session, ensure_session(account_id))
        mg = get_minigame(session)

    # Nick mới / session trống: không có user-token → tự refresh (không chỉ báo lỗi).
    if not mg.get("user_token"):
        prep = _auto_refresh_tokens_for_ws(
            session,
            account_id,
            game_key=game_key,
            reason="thiếu user-token",
        )
        mg = get_minigame(session)
        if prep.get("ok") and mg.get("ws_token"):
            return str(mg["ws_token"])
        err = prep.get("error") or "thiếu user-token"
        hint = f"chạy: python xoso66_minigame_refresh.py -a {account_id} --force"
        raise RuntimeError(f"không có ws_token sau refresh ({err}) — {hint}")

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
        elif rep.get("need_user_token_refresh") or not get_minigame(session).get(
            "user_token"
        ):
            prep = _auto_refresh_tokens_for_ws(
                session,
                account_id,
                game_key=game_key,
                reason="user-token hết hạn / getToken 401",
            )
            mg = get_minigame(session)
            if prep.get("ok") and mg.get("ws_token"):
                return str(mg["ws_token"])
            rep = prep.get("ws_token") if isinstance(prep.get("ws_token"), dict) else rep
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
            elif rep.get("need_user_token_refresh"):
                prep = _auto_refresh_tokens_for_ws(
                    session,
                    account_id,
                    game_key=game_key,
                    reason="user-token hết hạn / getToken 401",
                )
                mg = get_minigame(session)
                if prep.get("ok") and mg.get("ws_token"):
                    return str(mg["ws_token"])
                rep = (
                    prep.get("ws_token")
                    if isinstance(prep.get("ws_token"), dict)
                    else rep
                )

    mg = get_minigame(session)
    token = mg.get("ws_token")
    if not token:
        err = _format_ws_fetch_error(rep)
        hint = f"chạy: python xoso66_minigame_refresh.py -a {account_id} --force"
        raise RuntimeError(
            f"không có ws_token sau refresh{f' ({err})' if err else ''} — {hint}"
        )
    return str(token)
