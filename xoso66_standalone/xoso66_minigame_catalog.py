# -*- coding: utf-8 -*-
"""
Danh mục mini-game (merchant bet66 / mini-game.vip).

placeOrder (chung mọi game):
  POST https://www.mini-game.vip/server/order/placeOrder
  Header: user-token, cookie CF
  Body: {"game_id": N, "orders": [{"play_id": P, "price": 1000, "content": ""}]}
"""

from __future__ import annotations

from typing import Any

MERCHANT = "bet66"

def _plays_tai_xiu(tai: int, xiu: int) -> dict[str, dict[str, Any]]:
    return {
        "tai": {"play_id": tai, "code": "big"},
        "xiu": {"play_id": xiu, "code": "small"},
    }


GAMES: dict[str, dict[str, Any]] = {
    "taixiu_dai_loc": {
        "game_id": 9,
        "name": "Tài xỉu Đại Lộc",
        "gamename": "dice_md5",
        "sub_game_code": "dice2",
        "nav_id": 45,
        "plays": _plays_tai_xiu(126, 127),
    },
    "taixiu_17": {
        "game_id": 17,
        "name": "Tài xỉu Phát Tài",
        "gamename": "lobby",
        "plays": _plays_tai_xiu(1701, 1702),  # Tài 1701 — xác nhận placeOrder
    },
    "taixiu_18": {
        "game_id": 18,
        "name": "Tài xỉu 45 giây",
        "gamename": "lobby",
        "plays": _plays_tai_xiu(1801, 1802),  # Tài 1801 — xác nhận placeOrder
    },
    "taixiu_19": {
        "game_id": 19,
        "name": "Tài xỉu 60 giây",
        "gamename": "lobby",
        "plays": _plays_tai_xiu(1901, 1902),  # Tài 1901 — xác nhận placeOrder
    },
    "taixiu_2": {
        "game_id": 2,
        "name": "Tài xỉu",
        "gamename": "dice",
        "plays": _plays_tai_xiu(2, 3),  # Tài 2 — xác nhận placeOrder; Xỉu: 3 — chưa xác nhận
    },
}

GAME_ID_LABELS: dict[int, str] = {
    int(g["game_id"]): str(g["name"]) for g in GAMES.values()
}

DEFAULT_JACKPOT_GAME_IDS = tuple(sorted(GAME_ID_LABELS))


def game_by_key(key: str) -> dict[str, Any]:
    g = GAMES.get(key)
    if not g:
        raise KeyError(f"Không có game '{key}'. Có: {', '.join(GAMES)}")
    return g


def game_by_id(game_id: int) -> tuple[str, dict[str, Any]]:
    gid = int(game_id)
    for key, g in GAMES.items():
        if int(g.get("game_id") or 0) == gid:
            return key, g
    raise KeyError(f"Không có game_id={gid}. Có: {', '.join(str(i) for i in GAME_ID_LABELS)}")


def play_id_for_side(game_key: str, side: str) -> int:
    g = game_by_key(game_key)
    plays = g.get("plays") or {}
    p = plays.get(side)
    if not p:
        raise ValueError(f"side '{side}' không hợp lệ. Dùng: {', '.join(plays)}")
    return int(p["play_id"])


def place_order_body(
    game_key: str,
    *,
    side: str,
    amount: int,
    issue: str = "",
    content: str = "",
) -> dict[str, Any]:
    """Body JSON placeOrder (một cửa Tài/Xỉu)."""
    g = game_by_key(game_key)
    body: dict[str, Any] = {
        "game_id": int(g["game_id"]),
        "orders": [
            {
                "play_id": play_id_for_side(game_key, side),
                "price": int(amount),
                "content": content,
            }
        ],
    }
    if issue:
        body["issue"] = str(issue)
    return body
