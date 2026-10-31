# -*- coding: utf-8 -*-
"""
Thử nhận lì xì phòng chat — một lượt, không poll.

Chạy thật (chỉ khi được bảo):
    python run_chat_lixi_trial.py

Kiểm tra session local, không gọi game:
    python run_chat_lixi_trial.py --ready

Mỗi nick: GET getredpacketinfo một lần. Chỉ POST /chat/grabred khi red_notify
còn time_interval > 0. Không grab tin chat cũ trong getChatLog.
Nick đầu trả is_done / 1006 thì dừng các nick sau.
Không ghi session (không lưu profile android_app).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DIR))

ROSTER = DIR / "chat_lixi_trial_users.json"
RESULT = DIR / "chat_lixi_trial_result.json"
GAP_SEC = 0.3


def _load_roster() -> dict:
    return json.loads(ROSTER.read_text(encoding="utf-8"))


def _skip_ids(roster: dict) -> set[int]:
    out: set[int] = set()
    for raw in roster.get("skip_red_ids") or []:
        try:
            out.add(int(raw))
        except (TypeError, ValueError):
            continue
    last = roster.get("last_trial") or {}
    try:
        if last.get("red_id") is not None:
            out.add(int(last["red_id"]))
    except (TypeError, ValueError):
        pass
    return out


def _summarize_notify(items: list) -> list[dict]:
    rows = []
    for item in items:
        if not isinstance(item, dict):
            rows.append({"raw_type": type(item).__name__})
            continue
        rid = item.get("red_id") if item.get("red_id") is not None else item.get("id") or item.get("rid")
        rows.append(
            {
                "red_id": rid,
                "time_interval": item.get("time_interval"),
                "keys": sorted(item.keys()),
            }
        )
    return rows


def _slim_grab(g: dict) -> dict:
    data = g.get("data") if isinstance(g.get("data"), dict) else None
    return {
        "ok": bool(g.get("ok")),
        "code": g.get("code"),
        "msg": g.get("msg"),
        "red_id": g.get("red_id"),
        "money": g.get("money"),
        "data": data,
    }


def _packet_closed(grab: dict) -> bool:
    if grab.get("ok"):
        return False
    data = grab.get("data") if isinstance(grab.get("data"), dict) else {}
    if data.get("is_done") in (1, "1", True):
        return True
    return grab.get("code") in (1006, "1006")


def ready() -> int:
    from xoso66_accounts_db import get_account

    roster = _load_roster()
    print(f"skip_red_ids={sorted(_skip_ids(roster))}", flush=True)
    bad = 0
    for u in roster["users"]:
        acc = get_account(u["id"])
        sj = (acc or {}).get("session_json") or {}
        ok = bool(acc) and bool(str(sj.get("form_token") or "").strip()) and bool(str(acc.get("proxy") or sj.get("proxy") or "").strip())
        if not ok:
            bad += 1
        print(
            f"{'OK' if ok else 'THIEU'} {u['username']} {u['id']} "
            f"token={bool(str(sj.get('form_token') or '').strip())} "
            f"proxy={bool(str((acc or {}).get('proxy') or '').strip())} "
            f"cookies={bool(sj.get('cookies'))}",
            flush=True,
        )
    print(f"ready {len(roster['users']) - bad}/{len(roster['users'])}", flush=True)
    return 1 if bad else 0


def run(*, join_chat: bool = False, profile: str = "android_app") -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    from xoso66_accounts_db import account_to_session_dict, get_account
    from xoso66_chat_red_packet import (
        fetch_chat_log,
        fetch_chat_token,
        fetch_red_notify,
        grab_chat_red,
        pick_live_notify_red,
    )
    from xoso66_red_packet import apply_client_profile

    roster = _load_roster()
    skip = _skip_ids(roster)
    users = roster["users"]
    results = []
    stop = False
    for i, u in enumerate(users):
        row: dict = {"id": u["id"], "username": u["username"]}
        if stop:
            row["skipped"] = "bao truoc da dong"
            results.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            continue
        try:
            acc = get_account(u["id"])
            if not acc:
                row["error"] = "khong co account"
                results.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
                continue
            session = account_to_session_dict(acc)
            # Profile gắn trước mọi đọc — khớp thiết bị lúc grab.
            apply_client_profile(session, profile)
            if join_chat:
                tok = fetch_chat_token(session)
                log = fetch_chat_log(session, limit=100)
                row["join"] = {
                    "token_ok": tok.get("ok"),
                    "token_code": tok.get("code"),
                    "chat_ok": log.get("ok"),
                    "chat_code": log.get("code"),
                    "chat_list_len": len(log.get("list") or []),
                }
                # Web lấy red_notify từ getChatLog khi mở phòng.
                log_notify = []
                raw = log.get("raw") if isinstance(log.get("raw"), dict) else {}
                data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
                if isinstance(data.get("red_notify"), list):
                    log_notify = data["red_notify"]
                notify = log_notify if log_notify else fetch_red_notify(session)
            else:
                notify = fetch_red_notify(session)
            items = notify if isinstance(notify, list) else []
            row["notify"] = _summarize_notify(items)
            live = pick_live_notify_red(items, skip_ids=skip)
            row["live"] = (
                {"red_id": live["red_id"], "time_interval": live["time_interval"]} if live else None
            )
            if not live:
                row["grab"] = None
                row["reason"] = "khong co bao con time_interval > 0"
            else:
                grabbed = grab_chat_red(session, live["red_id"], client_profile=profile)
                row["grab"] = _slim_grab(grabbed)
                if _packet_closed(row["grab"]):
                    stop = True
                    row["stop_rest"] = True
        except Exception as e:
            row["error"] = f"{type(e).__name__}: {e}"
        results.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        if i + 1 < len(users) and not stop:
            time.sleep(GAP_SEC)
    RESULT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"WROTE {RESULT}", flush=True)
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--ready", action="store_true", help="Chỉ kiểm tra session local")
    ap.add_argument(
        "--join-chat",
        action="store_true",
        help="Trước grab: getToken + getChatLog (giống mở phòng). Vẫn tối đa ~3 request/nick.",
    )
    ap.add_argument("--profile", default="android_app", help="pc | android_app | android | ios")
    args = ap.parse_args()
    if args.ready:
        return ready()
    return run(join_chat=args.join_chat, profile=args.profile)


if __name__ == "__main__":
    raise SystemExit(main())
