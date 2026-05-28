# -*- coding: utf-8 -*-
"""
Probe WS mini-game: in payload open_info / bet_data có thông tin nổ hũ không.

  python xoso66_ws_jackpot_probe.py -a acc17778 --duration 120
  python xoso66_ws_jackpot_probe.py -u namchung1 --duration 90 --game-id 9

Ưu tiên nick có proxy, không bắt buộc «Đang Chơi». Chỉ 1 WS tạm — không đụng pool worker.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from xoso66_minigame_catalog import GAME_ID_LABELS
from xoso66_minigame_http import ws_url_from_token
from xoso66_minigame_session import get_ws_token
from xoso66_minigame_ws import (
    DEFAULT_WS_SUBSCRIBE,
    _connect_ws,
    _decode_message,
    _is_ping_payload,
    _pong_reply,
    _ts,
    _watch_gid,
    parse_ws_subscribe_spec,
    resolve_account_id,
    ws_ping_loop,
    ws_send_subscribes,
)
from xoso66_proxy import ensure_proxy
from xoso66_session import ensure_session, prep_site_session_before_ws

_JACKPOT_NUM_PATTERNS = (
    re.compile(r"^1\s*,\s*1\s*,\s*1$"),
    re.compile(r"^6\s*,\s*6\s*,\s*6$"),
    re.compile(r"^111$"),
    re.compile(r"^666$"),
)


def _normalize_dice(nums: str) -> str:
    s = str(nums or "").strip()
    if not s:
        return ""
    parts = [p.strip() for p in s.replace(" ", "").split(",") if p.strip()]
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return "".join(parts)
    return s.replace(" ", "").replace(",", "")


def _looks_like_jackpot_dice(nums: str) -> bool:
    s = str(nums or "").strip()
    if not s:
        return False
    if _normalize_dice(s) in ("111", "666"):
        return True
    for pat in _JACKPOT_NUM_PATTERNS:
        if pat.match(s):
            return True
    return False


def _interesting_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Trích field có thể liên quan nổ hũ / số người cược."""
    keys = (
        "game_id",
        "id",
        "issue",
        "open_numbers",
        "open_result",
        "is_jackpot",
        "jackpot",
        "money",
        "bet_num",
        "bet_count",
        "user_num",
        "player_num",
        "total_bet",
        "total_user",
        "big_bet",
        "small_bet",
        "big_count",
        "small_count",
        "bet_user",
        "next_info",
    )
    out: dict[str, Any] = {}
    for k in keys:
        if k in data:
            out[k] = data[k]
    res = data.get("open_result")
    if isinstance(res, dict):
        for k in ("result", "name", "sum", "is_jackpot", "jackpot", "money", "open_numbers"):
            if k in res and k not in out:
                out[f"open_result.{k}"] = res[k]
    return out


def _count_keys(obj: Any, depth: int = 0) -> list[str]:
    """Liệt kê key chứa bet/user/count/player/jackpot trong object."""
    found: list[str] = []
    if depth > 4:
        return found
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if any(
                x in kl
                for x in (
                    "jackpot",
                    "bet",
                    "user",
                    "player",
                    "count",
                    "num",
                    "total",
                    "pool",
                )
            ):
                found.append(f"{k}={v!r}"[:120])
            found.extend(_count_keys(v, depth + 1))
    elif isinstance(obj, list) and obj and depth < 3:
        found.extend(_count_keys(obj[0], depth + 1))
    return found


def _pick_probe_account(prefer_id: str = "") -> str:
    from xoso66_accounts_db import list_accounts

    if prefer_id:
        return prefer_id
    connected: set[str] = set()
    try:
        from xoso66_ws_pool import get_connected_ws_accounts, get_ws_task_accounts

        connected = set(get_ws_task_accounts()) | set(get_connected_ws_accounts())
    except Exception:
        pass
    candidates: list[tuple[str, str, str]] = []
    for row in list_accounts():
        aid = str(row.get("id") or "").strip()
        if not aid or not str(row.get("proxy") or "").strip():
            continue
        user = str(row.get("username") or aid)
        st = str(row.get("status") or "")
        if aid in connected:
            continue
        candidates.append((aid, user, st))
    if not candidates:
        for row in list_accounts():
            aid = str(row.get("id") or "").strip()
            if aid and str(row.get("proxy") or "").strip():
                return aid
        raise SystemExit("Không có account có proxy trong DB.")
    candidates.sort(key=lambda x: (0 if "Hết" in x[2] or x[2] == "" else 1, x[0]))
    aid, user, st = candidates[0]
    print(f"[PROBE] Chon {user} ({aid}) status={st!r} - khong trong WS pool", flush=True)
    return aid


async def run_probe(
    account_id: str,
    *,
    duration_sec: float,
    watch_gid: int | None,
    subscribe: str,
) -> int:
    from xoso66_accounts_db import get_account, username_for_log
    from xoso66_minigame_refresh import refresh_minigame_tokens

    await asyncio.to_thread(prep_site_session_before_ws, account_id)
    session = await asyncio.to_thread(ensure_session, account_id, force_login=False)
    await asyncio.to_thread(ensure_proxy, session)
    username = username_for_log(account_id, session)
    rep = await asyncio.to_thread(
        refresh_minigame_tokens,
        session,
        account_id=account_id,
        game_key="taixiu_dai_loc",
        force=True,
    )
    if not rep.get("ok"):
        print(f"[PROBE] refresh minigame lỗi: {rep.get('error') or rep}", flush=True)
        return 1
    tok = get_ws_token(session, account_id)
    if not tok:
        print(f"[PROBE] Không lấy được ws_token cho {username}", flush=True)
        return 1
    ws_url = ws_url_from_token(tok)
    print(f"[PROBE] {username} connect {ws_url[:60]}… subscribe={subscribe}", flush=True)

    proxy_str = str(session.get("proxy") or (get_account(account_id) or {}).get("proxy") or "")
    if not proxy_str:
        print(f"[PROBE] {username}: thieu proxy", flush=True)
        return 1
    ws, sock = await _connect_ws(ws_url, proxy_str)
    ping_stop = asyncio.Event()
    ping_gid = int(watch_gid or 9)
    ping_task = asyncio.create_task(ws_ping_loop(ws, ping_gid, stop=ping_stop))
    sub_plan = parse_ws_subscribe_spec(subscribe or DEFAULT_WS_SUBSCRIBE)
    await ws_send_subscribes(ws, sub_plan)

    deadline = time.time() + max(30.0, duration_sec)
    open_info_n = 0
    jackpot_hit_n = 0
    types_seen: dict[str, int] = {}

    try:
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=25.0)
            except asyncio.TimeoutError:
                print(f"[{_ts()}] (chờ message…)", flush=True)
                continue
            obj = _decode_message(raw)
            if not isinstance(obj, dict):
                continue
            if _is_ping_payload(obj):
                reply = _pong_reply(obj)
                if reply:
                    await ws.send(reply)
                continue
            t = str(obj.get("type") or "").lower()
            types_seen[t] = types_seen.get(t, 0) + 1
            data = obj.get("data") if isinstance(obj.get("data"), dict) else {}

            if t == "jackpot_money":
                print(
                    f"[{_ts()}] jackpot_money  {_interesting_fields(data)}",
                    flush=True,
                )
                continue

            if t in ("g_open_info", "open_info"):
                gid = _watch_gid(data)
                if watch_gid is not None and gid != watch_gid:
                    continue
                open_info_n += 1
                nums = str(data.get("open_numbers") or "")
                res = data.get("open_result") if isinstance(data.get("open_result"), dict) else {}
                is_jp = bool(res.get("is_jackpot"))
                dice_jp = _looks_like_jackpot_dice(nums)
                extra = _count_keys(data)
                if is_jp or dice_jp or open_info_n <= 3 or open_info_n % 10 == 0:
                    tag = []
                    if is_jp:
                        tag.append("is_jackpot=1")
                    if dice_jp:
                        tag.append("dice=111/666")
                    print(
                        f"[{_ts()}] open_info #{open_info_n} gid={gid} "
                        f"{' '.join(tag) or 'sample'}",
                        flush=True,
                    )
                    print(json.dumps(_interesting_fields(data), ensure_ascii=False), flush=True)
                    if extra:
                        print(f"  keys: {' | '.join(extra[:25])}", flush=True)
                if is_jp or dice_jp:
                    jackpot_hit_n += 1
                    out_path = _ROOT / "data" / f"ws_jackpot_probe_{account_id}.json"
                    out_path.write_text(
                        json.dumps(obj, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    print(f"  → saved {out_path.name}", flush=True)
                continue

            if t == "game_result":
                gid = _watch_gid(data)
                if watch_gid is None or gid == watch_gid:
                    print(
                        f"[{_ts()}] game_result  {json.dumps(obj, ensure_ascii=False)[:800]}",
                        flush=True,
                    )
                continue

            if t == "bet_data":
                gid = _watch_gid(data)
                if watch_gid is None or gid == watch_gid:
                    print(
                        f"[{_ts()}] bet_data  {json.dumps(data, ensure_ascii=False)[:500]}",
                        flush=True,
                    )
                continue

            if any(x in t for x in ("jackpot", "win", "settle", "prize")):
                print(
                    f"[{_ts()}] {t}  {json.dumps(obj, ensure_ascii=False)[:400]}",
                    flush=True,
                )
    finally:
        ping_stop.set()
        ping_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await ping_task
        with contextlib.suppress(Exception):
            await ws.close()
        with contextlib.suppress(Exception):
            sock.close()

    print(
        f"\n[PROBE] Done {username}: open_info={open_info_n} "
        f"jackpot_signals={jackpot_hit_n} types={dict(sorted(types_seen.items()))}",
        flush=True,
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Probe WS jackpot fields")
    ap.add_argument("-u", "--username", default="")
    ap.add_argument("-a", "--account", default="")
    ap.add_argument("--duration", type=float, default=120.0)
    ap.add_argument(
        "--game-id",
        type=int,
        default=9,
        help=f"chỉ log open_info game_id (mặc định 9 = {GAME_ID_LABELS.get(9, '?')})",
    )
    ap.add_argument("--subscribe", default=DEFAULT_WS_SUBSCRIBE)
    args = ap.parse_args()
    aid = resolve_account_id(username=args.username, account_id=args.account)
    if not aid:
        aid = _pick_probe_account()
    return asyncio.run(
        run_probe(
            aid,
            duration_sec=args.duration,
            watch_gid=args.game_id if args.game_id > 0 else None,
            subscribe=args.subscribe,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
