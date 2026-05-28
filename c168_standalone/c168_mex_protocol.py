# -*- coding: utf-8 -*-
"""Decode / phân loại frame WebSocket Bikimex–Mex (0xD9 + JSON, router)."""
from __future__ import annotations

import base64
import json
import re
from typing import Any

ROUND_START_RE = re.compile(
    r"roundstart|newround|startbet|betting|opencard|deal|countdown|"
    r"roundid|roundno|shoe|gamestart|bettime|placebet|openbet",
    re.I,
)
ROUND_RESULT_RE = re.compile(
    r"roundresult|gameresult|result|settle|draw|winner|banker|player|"
    r"card|roadmap|roundend|gameend|payout",
    re.I,
)
BET_SEND_RE = re.compile(
    r"^bet$|placebet|dobet|betting|wager|stake|addbet|confirm bet",
    re.I,
)
BET_RECV_RE = re.compile(
    r"betsuccess|betfail|betresult|betof|winresult|winlose|"
    r"betconfirm|betreply|betstatus|payout|winamount|lose",
    re.I,
)
_HINT_KEY_RE = re.compile(
    ROUND_START_RE.pattern + "|" + ROUND_RESULT_RE.pattern + "|" + BET_RECV_RE.pattern,
    re.I,
)
ROUND_HINT_RE = re.compile(
    ROUND_START_RE.pattern + "|" + ROUND_RESULT_RE.pattern + "|" + BET_RECV_RE.pattern,
    re.I,
)


def try_json(s: str) -> Any | None:
    s = (s or "").strip()
    if not s or s[0] not in "{[":
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


def decode_mex_frame(raw: str | bytes) -> tuple[str, Any | None]:
    """Frame Bikimex: 0xD9 + 1-byte len + JSON (hoặc text thuần)."""
    if isinstance(raw, str):
        if len(raw) > 2 and not raw.lstrip().startswith(("{", "[")):
            try:
                b = base64.b64decode(raw, validate=False)
                text, j = decode_mex_frame(b)
                if j is not None or text.startswith("{"):
                    return text, j
            except Exception:
                pass
            try:
                b = raw.encode("latin-1", errors="surrogateescape")
                text, j = decode_mex_frame(b)
                if j is not None or (text and text.lstrip().startswith("{")):
                    return text, j
            except Exception:
                pass
        idx = raw.find("{")
        if idx > 0:
            tail = raw[idx:]
            return tail, try_json(tail)
        return raw, try_json(raw)

    if len(raw) >= 3 and raw[0] == 0xD9:
        ln = raw[1]
        body = raw[2 : 2 + ln]
        if len(body) == ln:
            try:
                text = body.decode("utf-8")
                return text, try_json(text)
            except UnicodeDecodeError:
                pass
    try:
        text = raw.decode("utf-8")
        return text, try_json(text)
    except UnicodeDecodeError:
        return f"<binary {len(raw)}B>", None


def encode_mex_frame(obj: dict[str, Any]) -> bytes:
    body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    if len(body) > 255:
        raise ValueError("payload > 255 bytes")
    return bytes([0xD9, len(body)]) + body


def _router_name(obj: Any) -> str:
    if not isinstance(obj, dict):
        return ""
    return str(obj.get("router") or obj.get("cmd") or obj.get("action") or "")


def _bikimex_fields(obj: dict[str, Any]) -> dict[str, Any]:
    """Trích field từ JSON Bikimex (messageType / handler / eventType)."""
    mt = str(obj.get("messageType") or "")
    handler = obj.get("handler")
    msg = obj.get("message")
    inner: dict[str, Any] = msg if isinstance(msg, dict) else {}
    return {
        "messageType": mt,
        "handler": handler,
        "eventType": str(inner.get("eventType") or ""),
        "tableID": inner.get("tableID") or inner.get("tableId") or obj.get("tableID"),
        "tableName": inner.get("tableName"),
        "gameRound": inner.get("gameRound"),
        "gameShoe": inner.get("gameShoe"),
        "winner": inner.get("winner"),
        "iTime": inner.get("iTime"),
        "balance": obj.get("balance"),
    }


def _classify_bikimex(direction: str, obj: dict[str, Any]) -> str | None:
    """Phân loại frame game Bikimex; None = dùng heuristic cũ."""
    b = _bikimex_fields(obj)
    mt, et = b["messageType"], b["eventType"]
    handler = b.get("handler")

    if mt in ("Heartbeat", "Connecting", "ServerInfo", "Initialize"):
        return "heartbeat"
    if mt == "UserBalance":
        return "balance"

    if et == "GP_NEW_GAME_START":
        return "round_start"
    if et == "GP_WINNER":
        return "round_result"
    if et in ("GP_ONE_CARD_DRAWN", "GP_RANDOM_PAY"):
        return "deal"

    if mt == "GameHallInfo":
        if handler == 4 and et:
            return "round_start" if et == "GP_NEW_GAME_START" else (
                "round_result" if et == "GP_WINNER" else "deal"
            )
        return "lobby"

    if mt == "GameInfo":
        if handler == 4 and et:
            return "round_start" if et == "GP_NEW_GAME_START" else (
                "round_result" if et == "GP_WINNER" else "deal"
            )
        if handler == 1:
            return "bet_pool"
        if handler in (2, 3):
            return "table_meta"

    if direction == "send" and obj.get("router"):
        r = str(obj["router"])
        if r in ("heartbeat", "userPlainBalance", "serverInfo", "initialization"):
            return "heartbeat"
        if "streaming" in r:
            return "table_meta"
    return None


def _deep_find(obj: Any, key_re: re.Pattern, path: str = "", hits: list[tuple[str, Any]] | None = None) -> list[tuple[str, Any]]:
    if hits is None:
        hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if key_re.search(str(k)):
                hits.append((p, v))
            _deep_find(v, key_re, p, hits)
    elif isinstance(obj, list) and len(obj) <= 50:
        for i, v in enumerate(obj):
            _deep_find(v, key_re, f"{path}[{i}]", hits)
    return hits


def classify_ws_message(
    direction: str,
    text: str,
    obj: Any | None,
) -> str:
    """
    Trả nhãn: round_start | round_result | bet_send | bet_recv | other | unknown
    """
    d = (direction or "").lower()
    router = _router_name(obj) if obj is not None else ""
    blob = f"{router} {text[:400]}"

    if isinstance(obj, dict):
        bik = _classify_bikimex(d, obj)
        if bik:
            if bik == "heartbeat":
                return "other_send" if d == "send" else "other_recv"
            if bik == "lobby":
                return "lobby"
            if bik == "bet_pool":
                return "bet_pool"
            if bik == "table_meta":
                return "table_meta"
            if bik == "balance":
                return "balance"
            if bik == "deal":
                return "deal"
            return bik

    if d == "send":
        if BET_SEND_RE.search(router) or (obj and _deep_find(obj, BET_SEND_RE)):
            return "bet_send"
        if ROUND_START_RE.search(blob):
            return "round_start"
        return "other_send"

    if BET_RECV_RE.search(blob) or (obj and _deep_find(obj, BET_RECV_RE)):
        return "bet_recv"
    if ROUND_RESULT_RE.search(blob) or (obj and _deep_find(obj, ROUND_RESULT_RE)):
        return "round_result"
    if ROUND_START_RE.search(blob) or (obj and _deep_find(obj, ROUND_START_RE)):
        return "round_start"
    if BET_SEND_RE.search(router):
        return "bet_send"
    return "other_recv"


def summarize_message(category: str, direction: str, text: str, obj: Any | None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "category": category,
        "direction": direction,
        "router": _router_name(obj) if obj else "",
        "preview": text[:500],
    }
    if isinstance(obj, dict):
        b = _bikimex_fields(obj)
        for k, v in b.items():
            if v is not None and v != "":
                out[k] = v
        data = obj.get("data")
        if isinstance(data, dict):
            for k in (
                "roundId",
                "roundNo",
                "round",
                "gameRound",
                "shoeRound",
                "tableId",
                "table",
                "countDown",
                "countdown",
                "status",
                "phase",
                "result",
                "winner",
                "betAmount",
                "amount",
                "win",
                "balance",
            ):
                if k in data:
                    out[k] = data[k]
        for k in ("roundId", "roundNo", "tableId", "status", "result"):
            if k in obj and k not in out:
                out[k] = obj[k]
    hints = _deep_find(obj, ROUND_HINT_RE) if obj else []
    if hints:
        out["hints"] = [{"path": p, "value": v} for p, v in hints[:8]]
    return out


def table_id_from_obj(obj: dict[str, Any]) -> int | None:
    b = _bikimex_fields(obj)
    tid = b.get("tableID")
    if tid is not None:
        try:
            return int(tid)
        except (TypeError, ValueError):
            pass
    for key in ("tableInfo", "message"):
        block = obj.get(key)
        if isinstance(block, dict):
            t2 = block.get("tableID")
            if t2 is not None:
                try:
                    return int(t2)
                except (TypeError, ValueError):
                    pass
            inner = block.get("tableInfo")
            if isinstance(inner, dict) and inner.get("tableID") is not None:
                try:
                    return int(inner["tableID"])
                except (TypeError, ValueError):
                    pass
            road = block.get("roadInfo")
            if isinstance(road, dict) and road.get("tableID") is not None:
                try:
                    return int(road["tableID"])
                except (TypeError, ValueError):
                    pass
    return None


def _winner_vn(winner: Any) -> str:
    try:
        w = int(winner)
    except (TypeError, ValueError):
        return str(winner or "?")
    return {0: "Con thắng", 1: "Cái thắng", 2: "Hòa"}.get(w, f"winner={winner}")


def extract_baccarat_event(
    obj: dict[str, Any],
    *,
    table_id: int = 0,
) -> dict[str, Any] | None:
    """Chỉ GP_NEW_GAME_START / GP_WINNER (bàn table_id nếu > 0)."""
    tid = table_id_from_obj(obj)
    if table_id > 0 and tid is not None and int(tid) != int(table_id):
        return None
    b = _bikimex_fields(obj)
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    et = str(b.get("eventType") or msg.get("eventType") or "")
    shoe = int(b.get("gameShoe") or msg.get("gameShoe") or 0)
    rnd = int(b.get("gameRound") or msg.get("gameRound") or 0)
    if et == "GP_NEW_GAME_START" and shoe > 0 and rnd > 0:
        return {
            "kind": "round_start",
            "table_id": tid,
            "shoe": shoe,
            "round": rnd,
            "i_time": msg.get("iTime") or b.get("iTime"),
        }
    if et == "GP_WINNER" and rnd > 0:
        return {
            "kind": "round_result",
            "table_id": tid,
            "shoe": shoe,
            "round": rnd,
            "winner": msg.get("winner"),
            "player_val": msg.get("playerHandValue"),
            "banker_val": msg.get("bankerHandValue"),
        }
    return None


def format_baccarat_console(ev: dict[str, Any]) -> str:
    tid = ev.get("table_id")
    ban = f"bàn {tid} " if tid else ""
    if ev.get("kind") == "round_start":
        it = ev.get("i_time")
        cd = f" | đếm ngược {it}s" if it is not None else ""
        return (
            f"PHIEN MOI | {ban}shoe {ev.get('shoe')} ván {ev.get('round')}{cd}"
        )
    if ev.get("kind") == "round_result":
        w = _winner_vn(ev.get("winner"))
        extra = ""
        pv, bv = ev.get("player_val"), ev.get("banker_val")
        if pv is not None and bv is not None:
            extra = f" (Con {pv} — Cái {bv})"
        return f"KET QUA PHIEN | {ban}ván {ev.get('round')}: {w}{extra}"
    return ""


def format_event_line(summary: dict[str, Any]) -> str:
    cat = summary.get("category", "?")
    labels = {
        "round_start": "PHIEN MOI",
        "round_result": "KET QUA PHIEN",
        "deal": "CHIA BAI",
        "bet_pool": "POOL CUOC",
        "balance": "SO DU",
        "lobby": "SANH",
        "table_meta": "BAN",
        "bet_send": "DAT CUOC (gui)",
        "bet_recv": "KET QUA CUOC",
        "other_send": "gui",
        "other_recv": "nhan",
    }
    tag = labels.get(cat, cat)
    router = summary.get("router") or summary.get("messageType") or ""
    extra = []
    for k in (
        "tableID",
        "tableName",
        "gameRound",
        "gameShoe",
        "eventType",
        "winner",
        "balance",
        "roundId",
        "roundNo",
        "countDown",
        "status",
        "result",
    ):
        if k in summary:
            extra.append(f"{k}={summary[k]!r}")
    hint = f" router={router!r}" if router else ""
    ex = (" " + " ".join(extra)) if extra else ""
    prev = (summary.get("preview") or "")[:200]
    return f"[{tag}]{hint}{ex} {prev}"
