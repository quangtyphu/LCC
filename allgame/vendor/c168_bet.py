# -*- coding: utf-8 -*-
"""
Đặt cược BCR C168 qua HTTP addMyTransaction (tab vendor Chrome).

categoryIdx (API):
  1 = Player (Con)
  2 = Tie (Hòa)
  3 = Banker (Cái)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

SIDE_VN = {"player": "Con", "banker": "Cái", "tie": "Hòa"}

# categoryIdx → categoryName (theo API vendor)
BET_SIDE: dict[str, tuple[int, str]] = {
    "player": (1, "Player"),
    "tie": (2, "Tie"),
    "banker": (3, "Banker"),
}


def _standalone_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "c168_standalone"


def _import_standalone() -> None:
    d = _standalone_dir()
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))


def build_bet_form(
    *,
    table_id: int,
    game_shoe: int,
    game_round: int,
    stake: int | float,
    side: str = "player",
    bet_limit_id: int | str = 851101,
    f: str = "-1",
    c: str = "A",
) -> dict[str, str]:
    key = (side or "player").strip().lower()
    if key not in BET_SIDE:
        raise ValueError(f"side phải là {list(BET_SIDE)}")
    idx, name = BET_SIDE[key]
    data = json.dumps(
        [{"categoryIdx": idx, "categoryName": name, "stake": stake}],
        separators=(",", ":"),
    )
    return {
        "tableID": str(table_id),
        "gameShoe": str(game_shoe),
        "gameRound": str(game_round),
        "data": data,
        "betLimitID": str(bet_limit_id),
        "f": f,
        "c": c,
    }


def parse_bet_api_ok(out: dict[str, Any]) -> bool:
    if not out.get("ok"):
        return False
    parsed = out.get("parsed")
    if isinstance(parsed, dict):
        if parsed.get("status") in (0, "0", False):
            return False
        if parsed.get("success") is False:
            return False
    return True


_BET_JS = """
async (arg) => {
  const sides = {
    player: { idx: 1, name: 'Player' },
    tie: { idx: 2, name: 'Tie' },
    banker: { idx: 3, name: 'Banker' }
  };
  const side = sides[arg.side] || sides.player;
  const data = JSON.stringify([{
    categoryIdx: side.idx,
    categoryName: side.name,
    stake: Number(arg.stake)
  }]);
  const body = new URLSearchParams({
    tableID: String(arg.tableID),
    gameShoe: String(arg.gameShoe),
    gameRound: String(arg.gameRound),
    data,
    betLimitID: String(arg.betLimitID),
    f: String(arg.f ?? '-1'),
    c: String(arg.c ?? 'A')
  });
  const r = await fetch('/player/update/addMyTransaction', {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
      'X-Requested-With': 'XMLHttpRequest',
      'Accept': 'application/json, text/javascript, */*; q=0.01'
    },
    body: body.toString()
  });
  const text = await r.text();
  let parsed = null;
  try { parsed = JSON.parse(text); } catch (e) {}
  return {
    ok: r.ok,
    status: r.status,
    parsed,
    text: text.slice(0, 4000)
  };
}
"""


def place_bet_via_cdp(
    *,
    table_id: int,
    game_shoe: int,
    game_round: int,
    stake: int | float,
    side: str = "player",
    bet_limit_id: int | str = 851101,
    cdp_url: str,
    tab: dict[str, str] | None = None,
) -> dict[str, Any]:
    _import_standalone()
    from c168_vendor_bet import find_vendor_tab, _cdp_request  # type: ignore

    base = (cdp_url or "").rstrip("/")
    tab = tab or find_vendor_tab(base, require_table=True)
    if not tab:
        tab = find_vendor_tab(base, require_table=False)
    if not tab:
        return {"ok": False, "error": "no_vendor_tab", "hint": "Mở bàn Game B trong Chrome trước"}

    arg = {
        "tableID": table_id,
        "gameShoe": game_shoe,
        "gameRound": game_round,
        "stake": stake,
        "side": side.lower(),
        "betLimitID": bet_limit_id,
        "f": "-1",
        "c": "A",
    }
    wss = tab["wss"]
    _cdp_request(wss, "Runtime.enable", {}, 1)
    expr = f"({_BET_JS})({json.dumps(arg)})"
    resp = _cdp_request(
        wss,
        "Runtime.evaluate",
        {"expression": expr, "awaitPromise": True, "returnByValue": True},
        2,
    )
    result = resp.get("result") or {}
    if result.get("exceptionDetails"):
        return {
            "ok": False,
            "error": "js_exception",
            "tab": tab.get("url"),
            "details": result.get("exceptionDetails"),
        }
    value = (result.get("result") or {}).get("value")
    if isinstance(value, dict):
        value["tab"] = tab.get("url")
        value["form"] = build_bet_form(
            table_id=table_id,
            game_shoe=game_shoe,
            game_round=game_round,
            stake=stake,
            side=side,
            bet_limit_id=bet_limit_id,
        )
        return value
    return {"ok": False, "error": "unexpected_cdp_result", "raw": result, "tab": tab.get("url")}
