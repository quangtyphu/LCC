# -*- coding: utf-8 -*-
"""
Đặt cược baccarat vendor (Bikimex) qua HTTP — không qua WebSocket h54uk.

Endpoint (trong tab vendor, ví dụ bpcdf.vesnamex777.com):
  POST /player/update/addMyTransaction
  Content-Type: application/x-www-form-urlencoded

Body mẫu (từ DevTools):
  tableID=1006
  gameShoe=19992
  gameRound=46
  data=[{"categoryIdx":1,"categoryName":"Player","stake":20}]
  betLimitID=851101
  f=-1
  c=A

categoryIdx (HTTP) ≠ typeCode (WS GameInfo):
  WS typeCode 0 = Player; HTTP categoryIdx 1 = Player (1-based cho API này).

Gọi bet qua CDP (Chrome đang mở bàn — session + cookie JSESSIONID):
  python c168_vendor_bet.py --table 1006 --shoe 19992 --round 46 --stake 20 --side player --limit 851101
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from typing import Any
from urllib.parse import urlencode

try:
    import websocket as ws_lib
except ImportError:
    ws_lib = None  # type: ignore

from c168_capture_game_b import CDP_URL

BET_PATH = "/player/update/addMyTransaction"

# HTTP categoryIdx: 1=Player, 2=Tie, 3=Banker
BET_SIDE: dict[str, tuple[int, str]] = {
    "player": (1, "Player"),
    "tie": (2, "Tie"),
    "banker": (3, "Banker"),
}


def parse_bet_api_ok(out: dict[str, Any]) -> bool:
    """HTTP addMyTransaction: r.ok không đủ — kiểm tra JSON body."""
    if not isinstance(out, dict):
        return False
    parsed = out.get("parsed")
    if isinstance(parsed, dict):
        st = parsed.get("status")
        if st in (False, "FAIL", "fail", "Fail", "error", "ERROR"):
            return False
        if parsed.get("success") is False:
            return False
        ec = parsed.get("errorCode")
        if ec not in (None, "", 0, "0") and str(ec).lower() not in ("ok", "success"):
            try:
                if int(ec) != 0:
                    return False
            except (TypeError, ValueError):
                return False
        if st in (True, "OK", "ok", 200, "200", "success", "Success"):
            return True
        if parsed.get("success") is True:
            return True
    if out.get("ok") in (True, "true"):
        return True
    try:
        return int(out.get("status") or 0) == 200
    except (TypeError, ValueError):
        return False


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


def _fetch_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=8) as r:
        return json.loads(r.read().decode())


def _vendor_tab_score(url: str) -> int:
    """Ưu tiên singleBacTable; tránh gamehallBackToGame (cược qua đó dễ bị đẩy về sảnh)."""
    ul = (url or "").lower()
    if "singlebactable" in ul:
        return 100
    if "gamehallbacktogame" in ul:
        return 5
    if "webmain.jsp" in ul:
        return 12
    if "/player/" in ul:
        return 8
    if any(k in ul for k in ("bpcdf.", "tgmeq", "mhuxu", "bikimex", "vesnamex")):
        return 1
    return 0


def is_vendor_table_url(url: str) -> bool:
    return "singlebactable" in (url or "").lower()


def is_vendor_hall_redirect_url(url: str) -> bool:
    """Chỉ trang đẩy về sảnh — webMain vẫn có thể đang chơi bàn trong iframe."""
    return "gamehallbacktogame" in (url or "").lower()


def is_vendor_playable_url(url: str) -> bool:
    ul = (url or "").lower()
    if is_vendor_hall_redirect_url(url):
        return False
    return "singlebactable" in ul or "webmain.jsp" in ul


def find_vendor_tab(
    cdp_base: str = CDP_URL,
    *,
    require_table: bool = False,
    prefer_url_contains: str = "",
) -> dict[str, str] | None:
    """Tab vendor — ưu tiên singleBacTable; không có thì webMain (bàn trong iframe)."""
    try:
        tabs = _fetch_json(f"{cdp_base.rstrip('/')}/json/list")
    except Exception:
        return None
    if not isinstance(tabs, list):
        return None

    ranked: list[tuple[int, dict]] = []
    for t in tabs:
        if not isinstance(t, dict) or not t.get("webSocketDebuggerUrl"):
            continue
        url = str(t.get("url") or "")
        score = _vendor_tab_score(url)
        if score <= 0:
            continue
        ranked.append(
            (
                score,
                {
                    "url": url,
                    "wss": str(t["webSocketDebuggerUrl"]),
                    "score": score,
                },
            )
        )
    if not ranked:
        return None
    ranked.sort(key=lambda x: -x[0])
    pref = (prefer_url_contains or "").strip().lower()
    if pref:
        for score, tab in ranked:
            if pref in tab["url"].lower():
                return tab
    if require_table and ranked[0][0] < 12:
        return None
    return ranked[0][1]


def try_reenter_table_via_cdp(
    *,
    table_name: str,
    table_id: int,
    cdp_base: str = CDP_URL,
) -> dict[str, Any]:
    """Click lại C06 trên tab sảnh (gamehallBackToGame / webMain) — không F5."""
    from c168_vendor_enter_table import _JS_CLICK_LOBBY_TABLE

    tab = find_vendor_tab(cdp_base, require_table=False)
    if not tab:
        return {"ok": False, "error": "no_vendor_tab"}
    ul = tab["url"].lower()
    if is_vendor_table_url(tab["url"]):
        return {"ok": True, "already_on_table": True, "url": tab["url"]}
    if is_vendor_hall_redirect_url(tab["url"]):
        from c168_vendor_keepalive import click_back_to_game

        click_back_to_game(cdp_base)
        time.sleep(3)
        tab = find_vendor_tab(cdp_base, require_table=False) or tab
    elif _vendor_tab_score(tab["url"]) < 8:
        return {"ok": False, "error": "not_lobby_tab", "url": tab["url"]}

    wss = tab["wss"]
    _cdp_request(wss, "Runtime.enable", {}, 1)
    arg = json.dumps({"tableName": table_name, "tableId": table_id})
    resp = _cdp_request(
        wss,
        "Runtime.evaluate",
        {
            "expression": f"({ _JS_CLICK_LOBBY_TABLE })({arg})",
            "returnByValue": True,
        },
        2,
    )
    value = ((resp.get("result") or {}).get("result") or {}).get("value")
    time.sleep(5)
    on_table = find_vendor_tab(cdp_base, require_table=True)
    return {
        "ok": bool(on_table),
        "click": value,
        "from_url": tab["url"],
        "table_url": on_table.get("url") if on_table else None,
    }


def _cdp_request(wss: str, method: str, params: dict | None, msg_id: int, timeout: float = 20) -> dict:
    if ws_lib is None:
        raise RuntimeError("pip install websocket-client")
    ws = ws_lib.create_connection(wss, timeout=timeout)
    try:
        ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = ws.recv()
            msg = json.loads(raw)
            if msg.get("id") == msg_id:
                return msg
    finally:
        ws.close()
    return {}


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
    cdp_base: str = CDP_URL,
    tab: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Gửi cược trong context tab vendor (cookie JSESSIONID tự gắn).
    Cần Chrome mở đúng bàn (singleBacTable.jsp?dm=1).
    """
    tab = tab or find_vendor_tab(cdp_base, require_table=True)
    if not tab:
        tab = find_vendor_tab(cdp_base, require_table=False)
    if not tab:
        return {"ok": False, "error": "no_vendor_tab", "hint": "Mở bàn trong Chrome C168 trước"}
    if is_vendor_hall_redirect_url(tab["url"]):
        return {
            "ok": False,
            "error": "on_gamehall_not_table",
            "hint": "Tab gamehallBackToGame — click lại C06 trên sảnh",
            "tab": tab.get("url"),
        }

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


def place_bet_http(
    *,
    origin: str,
    cookies: dict[str, str],
    table_id: int,
    game_shoe: int,
    game_round: int,
    stake: int | float,
    side: str = "player",
    bet_limit_id: int | str = 851101,
    referer: str | None = None,
    proxy_server: str = "",
) -> dict[str, Any]:
    """POST trực tiếp (cần copy cookie JSESSIONID từ DevTools)."""
    form = build_bet_form(
        table_id=table_id,
        game_shoe=game_shoe,
        game_round=game_round,
        stake=stake,
        side=side,
        bet_limit_id=bet_limit_id,
    )
    url = origin.rstrip("/") + BET_PATH
    ref = referer or f"{origin.rstrip('/')}/player/singleBacTable.jsp?dm=1"
    body = urlencode(form).encode("utf-8")
    cookie_hdr = "; ".join(f"{k}={v}" for k, v in cookies.items())
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Origin": origin.rstrip("/"),
            "Referer": ref,
            "Cookie": cookie_hdr,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
            ),
        },
    )
    opener: urllib.request.OpenerDirector | None = None
    if proxy_server and str(proxy_server).strip():
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(
                {
                    "http": proxy_server.strip(),
                    "https": proxy_server.strip(),
                }
            )
        )
    try:
        if opener:
            r = opener.open(req, timeout=25)
        else:
            r = urllib.request.urlopen(req, timeout=25)
        with r:
            text = r.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            out = {
                "ok": True,
                "status": r.status,
                "parsed": parsed,
                "text": text[:4000],
                "form": form,
                "via": "http",
            }
            out["ok"] = parse_bet_api_ok(out)
            return out
    except Exception as exc:
        return {"ok": False, "error": str(exc), "form": form, "via": "http"}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Đặt cược vendor C168 qua CDP (Chrome đang mở bàn)")
    ap.add_argument("--table", type=int, default=1006)
    ap.add_argument("--shoe", type=int, required=True)
    ap.add_argument("--round", type=int, required=True)
    ap.add_argument("--stake", type=float, default=20)
    ap.add_argument("--side", choices=list(BET_SIDE), default="player")
    ap.add_argument("--limit", type=int, default=851101, help="betLimitID")
    ap.add_argument("--cdp", default=CDP_URL)
    args = ap.parse_args()

    tab = find_vendor_tab(args.cdp)
    if tab:
        print(f"Tab vendor: {tab['url'][:100]}", flush=True)
    else:
        print("Không thấy tab vendor — vẫn thử CDP…", flush=True)

    out = place_bet_via_cdp(
        table_id=args.table,
        game_shoe=args.shoe,
        game_round=args.round,
        stake=args.stake,
        side=args.side,
        bet_limit_id=args.limit,
        cdp_base=args.cdp,
        tab=tab,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
