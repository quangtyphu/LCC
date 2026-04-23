# session_game_total.py
"""
Chỉ lấy tổng cược toàn game (một bên Tài/Xỉu) qua API session-summary.

Tự chọn tài khoản để gọi API (theo thứ tự):
  1) User đang mở WS trong process bot (active_ws)
  2) SQLite user_profiles: status = Đang Chơi + đủ proxy/jwt/accessToken
  3) CMS GET /api/users: Đang Chơi + đủ token
  4) SQLite: bất kỳ user có đủ token (fallback, in cảnh báo)

Hoặc chỉ định: --user <username>

Tổng cược game = betSummaries[].totalAmount (Tài = Xỉu) hoặc overall.totalAmount / 2.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from typing import Any

import requests

from check_jackpot_session import _game_total_one_side, fetch_session_summary
from game_api_helper import NODE_SERVER_URL
from jackpot_session_db import DB_PATH


def _row_has_auth(row: dict) -> bool:
    p = (row.get("proxy") or "").strip()
    j = (row.get("jwt") or "").strip()
    a = (row.get("accessToken") or "").strip()
    return bool(p and j and a)


def pick_from_active_ws() -> str | None:
    """User đang có WS trong cùng process, bỏ qua bản ghi chỉ connecting."""
    try:
        from constants import active_ws
    except ImportError:
        return None
    if not active_ws:
        return None
    for user, meta in active_ws.items():
        if not isinstance(meta, dict):
            return str(user)
        if meta.get("connecting") and meta.get("task") is None:
            continue
        return str(user)
    return str(next(iter(active_ws.keys())))


def pick_playing_from_sqlite() -> str | None:
    if not os.path.isfile(DB_PATH):
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT username FROM user_profiles
            WHERE status = 'Đang Chơi'
              AND proxy IS NOT NULL AND TRIM(proxy) != ''
              AND jwt IS NOT NULL AND TRIM(jwt) != ''
              AND accessToken IS NOT NULL AND TRIM(accessToken) != ''
            ORDER BY username
            LIMIT 1
            """
        )
        row = cur.fetchone()
        conn.close()
        return str(row[0]) if row and row[0] else None
    except sqlite3.Error:
        return None


def pick_playing_from_cms_api() -> str | None:
    try:
        r = requests.get(f"{NODE_SERVER_URL}/api/users", timeout=8)
        if r.status_code != 200:
            return None
        users = r.json()
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(users, list):
        return None
    candidates = [
        u for u in users
        if isinstance(u, dict)
        and u.get("status") == "Đang Chơi"
        and _row_has_auth(u)
    ]
    candidates.sort(key=lambda u: str(u.get("username") or ""))
    if not candidates:
        return None
    return str(candidates[0]["username"])


def pick_any_token_from_sqlite() -> str | None:
    """Fallback: một user bất kỳ có đủ token (khi không ai Đang Chơi)."""
    if not os.path.isfile(DB_PATH):
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT username FROM user_profiles
            WHERE proxy IS NOT NULL AND TRIM(proxy) != ''
              AND jwt IS NOT NULL AND TRIM(jwt) != ''
              AND accessToken IS NOT NULL AND TRIM(accessToken) != ''
            ORDER BY username
            LIMIT 1
            """
        )
        row = cur.fetchone()
        conn.close()
        return str(row[0]) if row and row[0] else None
    except sqlite3.Error:
        return None


def pick_any_online_username(*, warn_fallback: bool = True) -> tuple[str | None, str | None]:
    """
    Returns:
        (username, nguồn chọn: 'ws' | 'db_playing' | 'api_playing' | 'db_any' | None)
    """
    u = pick_from_active_ws()
    if u:
        return u, "ws"
    u = pick_playing_from_sqlite()
    if u:
        return u, "db_playing"
    u = pick_playing_from_cms_api()
    if u:
        return u, "api_playing"
    u = pick_any_token_from_sqlite()
    if u:
        if warn_fallback:
            print(
                "⚠️ Không có tài khoản 'Đang Chơi'; dùng user đầu tiên có JWT trong DB.",
                flush=True,
            )
        return u, "db_any"
    return None, None


_PICK_SOURCE_LABEL = {
    "ws": "bot WebSocket (active_ws)",
    "db_playing": "SQLite — Đang Chơi",
    "api_playing": "CMS /api/users — Đang Chơi",
    "db_any": "SQLite — có JWT (fallback)",
    "cli": "tham số --user",
}


def get_session_game_total_bet(
    session_id: int | str,
    username: str | None = None,
    *,
    log_pick: bool = True,
) -> tuple[float | None, dict[str, Any] | None]:
    """
    Returns:
        (tổng cược một bên toàn game, toàn bộ JSON session-summary hoặc None)
    """
    explicit = (username or "").strip()
    if explicit:
        user, src = explicit, "cli"
    else:
        user, src = pick_any_online_username(warn_fallback=True)
    if not user:
        print(
            "❌ Không tìm được tài khoản có JWT/proxy/accessToken. "
            "Kiểm tra CMS + game_data.db, hoặc truyền --user.",
            flush=True,
        )
        return None, None
    if log_pick and src:
        label = _PICK_SOURCE_LABEL.get(src, src)
        print(f"📌 Gọi session-summary với user [{user}] ({label})", flush=True)
    summary = fetch_session_summary(user, session_id)
    if not summary:
        return None, None
    total = _game_total_one_side(summary.get("overall"))
    if total is None:
        print("❌ Response không có overall để tính tổng cược game.", flush=True)
    return total, summary


def main() -> None:
    p = argparse.ArgumentParser(
        description="Lấy tổng cược toàn game (1 bên) từ session-summary"
    )
    p.add_argument("session_id", type=int, help="ID phiên (vd: 6730130)")
    p.add_argument(
        "--user",
        dest="username",
        default="",
        help="Ép dùng username này; mặc định tự chọn (WS → Đang Chơi DB/API → fallback)",
    )
    args = p.parse_args()
    total, summary = get_session_game_total_bet(args.session_id, args.username or None)
    if total is None:
        sys.exit(1)
    sid = summary.get("id") if summary else args.session_id
    ts = (summary or {}).get("timestamp")
    res = (summary or {}).get("resultTruyenThong")
    print(f"Phiên: {sid}", flush=True)
    if res:
        print(f"Kết quả: {res}", flush=True)
    if ts:
        print(f"Thời gian: {ts}", flush=True)
    print(f"Tổng cược toàn game (một bên): {total:,.2f}", flush=True)


if __name__ == "__main__":
    main()
