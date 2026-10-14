# -*- coding: utf-8 -*-
"""
Lì xì / phong bì đỏ trong **phòng chat** (khác lì xì sự kiện 21h).

Frontend (bundle index.*.js, 2026-10):
  - Tin chat / WS: type ``chat_red`` hoặc ``chat_red_record`` → ``data.red_id``.
  - Popup nhỏ trong khung chat → ``getChatGrabred()`` → ``chat/chatGrabred``.
  - Web chặn **client**: ``device_type != 0`` và ``device_type_data`` không có ``"pc"``
    → popup 「Hiện lì xì chỉ được phát trên APP」 (không gọi API).
  - API nhận (bypass UI): POST ``/server/chat/grabred`` body ``{ "red_id": <int> }``.

Lì xì sự kiện (sidebar / mưa / 21h) — KHÔNG dùng module này:
  GET ``/server/user/getredpacketinfo``, POST ``/server/user/grabredpacket`` (``rid``).

Phong bì chat sống rất ngắn → chạy ``watch`` (poll ``red_notify`` + ``getChatLog``), thấy ``red_id`` thì ``grab`` ngay.

CLI (1 nick):
  python xoso66_chat_red_packet.py gianghoahoa
  python xoso66_chat_red_packet.py gianghoahoa --interval 0.8
  python xoso66_chat_red_packet.py gianghoahoa grab --red-id 123456
  python xoso66_chat_red_packet.py gianghoahoa notify
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Callable

from xoso66_red_packet import (
    _parse_api_body,
    apply_client_profile,
    fetch_red_packet_info,
    resolve_account_ids,
)

PATH_CHAT_GRAB = "/server/chat/grabred"
PATH_CHAT_LOG = "/server/chat/getChatLog"

# Phòng chat chính (sniff gianghoahoa 2026-10-05 — ``chat/getChatLog`` Vue dispatch).
DEFAULT_CHAT_GROUP_ID = "928D69AEE182CFA576837902F1FC5D8E"

# Site chặn PC trên UI; grab HTTP nên giả app (giống xoso66_red_packet).
DEFAULT_GRAB_PROFILE = "android_app"

CHAT_RED_TYPES = frozenset({"chat_red", "chat_red_record"})


def chat_group_id(session: dict) -> str:
    """group_id phòng chat — ưu tiên env/config sau này; hiện default site."""
    ui = session.get("user_info") or {}
    gid = str(ui.get("chat_group_id") or ui.get("group_id") or "").strip()
    if gid and gid not in ("0", "None"):
        return gid
    return DEFAULT_CHAT_GROUP_ID


def parse_chat_red_payload(msg: dict[str, Any]) -> dict[str, Any] | None:
    """
    Trích red_id từ 1 message (getChatLog item hoặc WS ``chat/messageProcess``).

    getChatLog: ``{"type":"chat_red","time":...,"data":{...}}``
    WS: cùng shape hoặc ``{"type":"chat_red","data":{...}}``.
    """
    if not isinstance(msg, dict):
        return None
    typ = str(msg.get("type") or "").strip()
    if typ not in CHAT_RED_TYPES:
        inner = msg.get("payload") if isinstance(msg.get("payload"), dict) else None
        if inner and str(inner.get("type") or "") in CHAT_RED_TYPES:
            msg = inner
            typ = str(msg.get("type") or "")
        else:
            return None
    data = msg.get("data")
    if not isinstance(data, dict):
        return None
    rid = data.get("red_id")
    if rid is None:
        return None
    try:
        red_id = int(rid)
    except (TypeError, ValueError):
        return None
    device_type = data.get("device_type")
    device_type_data = data.get("device_type_data")
    if isinstance(device_type_data, str):
        try:
            device_type_data = json.loads(device_type_data)
        except Exception:
            device_type_data = [device_type_data]
    if not isinstance(device_type_data, list):
        device_type_data = device_type_data or []
    pc_allowed = int(device_type or 0) == 0 or "pc" in device_type_data
    return {
        "red_id": red_id,
        "type": typ,
        "time": msg.get("time"),
        "title": data.get("title"),
        "device_type": device_type,
        "device_type_data": device_type_data,
        "pc_allowed_on_web": pc_allowed,
        "chat_id": data.get("chat_id"),
        "raw_data": data,
    }


def fetch_chat_log(
    session: dict,
    *,
    group_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    from xoso66_bank_bind import _get_encrypted

    gid = group_id or chat_group_id(session)
    _, raw = _get_encrypted(
        session,
        PATH_CHAT_LOG,
        {"group_id": gid, "limit": int(limit)},
    )
    out = _parse_api_body(raw)
    data = out.get("data") if isinstance(out, dict) else None
    lst: list[Any] = []
    if isinstance(data, dict):
        lst = list(data.get("list") or [])
    elif isinstance(data, list):
        lst = data
    return {
        "ok": out.get("code") == 1 if isinstance(out, dict) else False,
        "code": out.get("code") if isinstance(out, dict) else None,
        "msg": out.get("msg") if isinstance(out, dict) else None,
        "group_id": gid,
        "list": lst,
        "raw": out,
    }


def find_chat_red_in_log(session: dict, **kwargs: Any) -> list[dict[str, Any]]:
    """Quét getChatLog — trả mọi phong bì ``chat_red*`` còn trong limit tin."""
    log = fetch_chat_log(session, **kwargs)
    found: list[dict[str, Any]] = []
    for item in log.get("list") or []:
        if not isinstance(item, dict):
            continue
        parsed = parse_chat_red_payload(item)
        if parsed:
            found.append(parsed)
    return found


def fetch_red_notify(session: dict) -> list[Any]:
    """``getredpacketinfo.data.red_notify`` — danh sách lì xì đang notify (có thể kèm chat)."""
    info = fetch_red_packet_info(session)
    notify = info.get("red_notify")
    if notify is None and isinstance(info.get("raw"), dict):
        data = (info["raw"].get("data") or {}) if isinstance(info["raw"], dict) else {}
        notify = data.get("red_notify")
    return list(notify or [])


def _red_id_done(grab: dict[str, Any]) -> bool:
    if grab.get("ok"):
        return True
    raw = grab.get("raw") if isinstance(grab.get("raw"), dict) else {}
    data = raw.get("data") if isinstance(raw.get("data"), dict) else grab.get("data")
    if isinstance(data, dict) and data.get("is_done") in (1, "1", True):
        return True
    code = grab.get("code")
    return False


def collect_active_red_ids(session: dict, *, limit: int = 100) -> list[dict[str, Any]]:
    """Gộp red_id từ red_notify (ưu tiên) + tin chat_red trong log."""
    out: dict[int, dict[str, Any]] = {}
    for item in fetch_red_notify(session):
        if not isinstance(item, dict):
            continue
        rid = item.get("red_id") or item.get("id") or item.get("rid")
        try:
            red_id = int(rid)
        except (TypeError, ValueError):
            continue
        out[red_id] = {"red_id": red_id, "source": "red_notify", "meta": item}
    for row in find_chat_red_in_log(session, limit=limit):
        rid = int(row["red_id"])
        if rid not in out:
            out[rid] = {"red_id": rid, "source": "chat_log", "meta": row}
    return list(out.values())


def grab_chat_red(
    session: dict,
    red_id: int | str,
    *,
    client_profile: str | None = DEFAULT_GRAB_PROFILE,
) -> dict[str, Any]:
    """POST /server/chat/grabred — không qua popup web."""
    from xoso66_session import post_encrypted

    if client_profile:
        apply_client_profile(session, client_profile)
    rid = int(red_id)
    _, raw, _ = post_encrypted(session, PATH_CHAT_GRAB, {"red_id": rid})
    out = _parse_api_body(raw)
    data = out.get("data") if isinstance(out, dict) else None
    money = None
    if isinstance(data, dict):
        for k in ("money", "amount", "receive_money"):
            if data.get(k) is not None:
                money = data.get(k)
                break
    return {
        "ok": out.get("code") == 1 if isinstance(out, dict) else False,
        "code": out.get("code") if isinstance(out, dict) else None,
        "msg": out.get("msg") if isinstance(out, dict) else None,
        "red_id": rid,
        "money": money,
        "data": data,
        "raw": out,
        "client_profile": client_profile,
    }


def watch_chat_red_loop(
    account_id: str,
    session: dict,
    *,
    interval_sec: float = 1.0,
    limit: int = 100,
    client_profile: str = DEFAULT_GRAB_PROFILE,
    should_stop: Callable[[], bool] | None = None,
    on_persist: Callable[[], None] | None = None,
) -> None:
    """
    Vòng lặp: poll red_notify + chat log → POST grabred ngay khi thấy red_id mới.
    ``finished`` = đã nhận thành công hoặc server báo hết / không còn (is_done).
    """
    from xoso66_session import persist_session

    user = (session.get("user_info") or {}).get("username") or session.get("username") or account_id
    finished: set[int] = set()
    announced: set[int] = set()

    def _stop() -> bool:
        if should_stop and should_stop():
            return True
        try:
            from xoso66_shutdown import stopping

            return stopping()
        except Exception:
            return False

    print(
        f"[CHAT-LIXI] watch user={user} aid={account_id} "
        f"group={chat_group_id(session)} interval={interval_sec}s profile={client_profile}",
        flush=True,
    )
    while not _stop():
        try:
            for row in collect_active_red_ids(session, limit=limit):
                rid = int(row["red_id"])
                if rid in finished:
                    continue
                if rid not in announced:
                    announced.add(rid)
                    meta = row.get("meta") or {}
                    print(
                        f"[CHAT-LIXI] thay red_id={rid} source={row.get('source')} "
                        f"meta={json.dumps(meta, ensure_ascii=False)[:350]}",
                        flush=True,
                    )
                g = grab_chat_red(session, rid, client_profile=client_profile)
                if g.get("ok"):
                    print(
                        f"[CHAT-LIXI] OK red_id={rid} money={g.get('money')} msg={g.get('msg')!r}",
                        flush=True,
                    )
                    finished.add(rid)
                    if on_persist:
                        on_persist()
                    else:
                        persist_session(account_id, session)
                elif _red_id_done(g):
                    print(
                        f"[CHAT-LIXI] het red_id={rid} code={g.get('code')} msg={g.get('msg')!r}",
                        flush=True,
                    )
                    finished.add(rid)
                else:
                    print(
                        f"[CHAT-LIXI] retry red_id={rid} code={g.get('code')} msg={g.get('msg')!r}",
                        flush=True,
                    )
        except KeyboardInterrupt:
            print("\n[CHAT-LIXI] dung (Ctrl+C).", flush=True)
            return
        except Exception as e:
            print(f"[CHAT-LIXI] loi: {e}", flush=True)
        time.sleep(max(0.3, float(interval_sec)))
    print("[CHAT-LIXI] worker dung.", flush=True)


def scan_chat_red_loop(
    account_id: str,
    session: dict,
    *,
    interval_sec: float = 2.0,
    limit: int = 100,
    auto_grab: bool = False,
    seen: set[int] | None = None,
) -> None:
    """CLI legacy — prefer watch_chat_red_loop."""
    if auto_grab:
        watch_chat_red_loop(
            account_id,
            session,
            interval_sec=interval_sec,
            limit=limit,
        )
        return
    seen_ids = seen if seen is not None else set()
    print(
        f"[scan] aid={account_id} group={chat_group_id(session)} "
        f"interval={interval_sec}s limit={limit} auto_grab={auto_grab}",
        flush=True,
    )
    while True:
        try:
            for row in collect_active_red_ids(session, limit=limit):
                rid = int(row["red_id"])
                if rid in seen_ids:
                    continue
                seen_ids.add(rid)
                print(f"[scan] red_id={rid} source={row.get('source')}", flush=True)
        except KeyboardInterrupt:
            print("\n[scan] dung.", flush=True)
            return
        except Exception as e:
            print(f"[scan] loi: {e}", flush=True)
        time.sleep(max(0.5, float(interval_sec)))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(
        description="Lì xì phòng chat — 1 nick: mặc định watch + auto grab",
    )
    ap.add_argument("target", help="username hoặc acc id (vd. gianghoahoa)")
    ap.add_argument(
        "cmd",
        nargs="?",
        default="watch",
        choices=("watch", "scan", "grab", "notify", "dump-log"),
        help="watch = nghe + nhận (mặc định)",
    )
    ap.add_argument("--interval", type=float, default=1.0, help="Giây giữa mỗi vòng poll")
    ap.add_argument("--limit", type=int, default=100, help="Số tin chat log quét")
    ap.add_argument(
        "--profile",
        default=DEFAULT_GRAB_PROFILE,
        help="pc | android | android_app (mặc định android_app)",
    )
    ap.add_argument("--red-id", type=int, help="Bắt buộc khi cmd=grab")
    ap.add_argument(
        "--auto-grab",
        action="store_true",
        help="cmd=scan: tự grab (watch đã grab sẵn)",
    )

    args = ap.parse_args()
    aids = resolve_account_ids(args.target.strip())
    if not aids:
        print(f"Khong tim thay acc: {args.target}", flush=True)
        return 2
    aid = aids[0]

    from xoso66_session import ensure_session, persist_session

    session = ensure_session(aid)
    user = (session.get("user_info") or {}).get("username") or session.get("username")

    if args.cmd == "notify":
        notify = fetch_red_notify(session)
        print(json.dumps({"user": user, "red_notify": notify}, ensure_ascii=False, indent=2), flush=True)
        return 0

    if args.cmd == "dump-log":
        rows = find_chat_red_in_log(session)
        print(json.dumps({"user": user, "chat_red": rows}, ensure_ascii=False, indent=2), flush=True)
        return 0

    if args.cmd == "grab":
        if args.red_id is None:
            print("Can --red-id khi dung grab", flush=True)
            return 2
        r = grab_chat_red(session, args.red_id, client_profile=args.profile)
        persist_session(aid, session)
        print(json.dumps({"user": user, **r}, ensure_ascii=False, indent=2), flush=True)
        return 0 if r.get("ok") else 1

    if args.cmd == "watch":
        watch_chat_red_loop(
            aid,
            session,
            interval_sec=args.interval,
            limit=args.limit,
            client_profile=args.profile,
            on_persist=lambda: persist_session(aid, session),
        )
        persist_session(aid, session)
        return 0

    if args.cmd == "scan":
        scan_chat_red_loop(
            aid,
            session,
            interval_sec=args.interval,
            limit=args.limit,
            auto_grab=args.auto_grab,
        )
        persist_session(aid, session)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
