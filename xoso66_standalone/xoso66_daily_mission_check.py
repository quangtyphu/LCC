# -*- coding: utf-8 -*-
"""
Kiểm tra + nhận thưởng nhiệm vụ — mission/list + mission/reward.

Map level_id → mission_id:
  114–120 → mission_id 17 (MINI GAME 7 ngày)
  161     → mission_id 22 (Điểm danh mỗi ngày)

status (levelList): 0 = chưa đủ ĐK, 1 = được nhận (gọi reward), 2 = đã nhận.

Chạy:
  python xoso66_daily_mission_check.py user1 user2
  python xoso66_daily_mission_check.py user1 --check-only   # chỉ xem, không nhận
  python xoso66_daily_mission_check.py user1 --signs
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from xoso66_accounts_db import (
    get_account_by_username,
    init_db,
    list_accounts,
    list_accounts_by_status,
)
from xoso66_deposit import (
    BASE_URL,
    apply_response_tokens,
    build_common_headers,
    get_form_token,
    _game_http,
)
from xoso66_session import _merge_response_cookies, ensure_session, persist_session
from xoso66_config_util import configure_stdio_utf8, safe_print

configure_stdio_utf8()
print = safe_print  # noqa: A001 — log tiếng Việt an toàn trên Windows/CMS API

MISSION_LIST_PATH = "/server/mission/list"
MISSION_REWARD_PATH = "/server/mission/reward"


def _require_crypto() -> str | None:
    """Trả message lỗi nếu chưa cài pycryptodome (cần cho login/API)."""
    try:
        from xoso66_deposit import crypto_available

        if crypto_available():
            return None
    except Exception:
        pass
    return "Thiếu pycryptodome — chạy: pip install pycryptodome"


SIGN_ID_DAILY_MISSION = 22
SIGN_ID_MINI_WEEK = 17
DAILY_LEVEL_ID = 161
LEVEL_IDS_WEEK = tuple(range(114, 121))
LEVEL_MISSION_MAP: dict[int, int] = {
    **{lid: SIGN_ID_MINI_WEEK for lid in LEVEL_IDS_WEEK},
    DAILY_LEVEL_ID: SIGN_ID_DAILY_MISSION,
}
TRACK_SIGN_IDS = (SIGN_ID_MINI_WEEK, SIGN_ID_DAILY_MISSION)
REWARD_CLAIM_STATUS = 1
# Site trả 888888 trên level 161 khi đủ cược ngày — không poll chờ bắt kịp DB.
DAILY_161_DONE_BET_COMPLETE = 888_888


def is_daily_161_complete_sentinel(done_bet_money: int | float) -> bool:
    """Site trả 888888 trên level 161 — coi đủ cược ngày."""
    return int(done_bet_money or 0) >= DAILY_161_DONE_BET_COMPLETE


def needs_daily_161_bet_poll(
    done_bet_money: int | float, daily_bet_total: int | float
) -> bool:
    """
    Poll chờ mission 161 chỉ khi cả hai đều đúng:
    done_bet < 888888 VÀ done_bet < tổng cược ngày.
    """
    done = int(done_bet_money or 0)
    total = int(daily_bet_total or 0)
    if total <= 0:
        return False
    return done < DAILY_161_DONE_BET_COMPLETE and done < total


def is_mission_reward_rate_limit(msg: str) -> bool:
    """Site: «Vui lòng không gửi lệnh nhiều lần, mời tải lại trang và thử lại»."""
    m = str(msg or "").strip().lower()
    if not m:
        return False
    return "không gửi lệnh nhiều lần" in m or (
        "nhiều lần" in m and "tải lại trang" in m
    )


def is_mission_reward_ip_already_claimed(msg: str) -> bool:
    """Site: «IP này đã nhận thưởng» — giới hạn theo outbound IP/proxy, không retry poll."""
    m = str(msg or "").strip().lower()
    if not m:
        return False
    return "ip" in m and "đã nhận" in m and "thưởng" in m


def fetch_mission_list(session: dict) -> dict[str, Any]:
    """GET /server/mission/list — cookie + form-token + CF headers."""
    form_token = get_form_token(session)
    headers = build_common_headers(
        session,
        form_token=form_token,
        content_type="application/json",
    )
    url = f"{BASE_URL}{MISSION_LIST_PATH}"
    r = _game_http(session).get(url, headers=headers, timeout=35)
    apply_response_tokens(session, r.headers)
    _merge_response_cookies(session, r)
    try:
        js = r.json()
    except Exception:
        return {
            "ok": False,
            "http_status": r.status_code,
            "raw": r.text[:600],
        }
    ok = r.status_code == 200 and js.get("code") == 1
    data = js.get("data")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        data = {"_raw": data}
    return {
        "ok": ok,
        "http_status": r.status_code,
        "code": js.get("code"),
        "msg": js.get("msg"),
        "data": data,
        "raw": js,
    }


def _reward_error_msg(http_status: int, js: Any, raw_text: str) -> str:
    if isinstance(js, dict):
        msg = js.get("msg")
        if msg:
            return str(msg)
        code = js.get("code")
        if code is not None:
            return f"code={code}"
        if js.get("_decrypt_error"):
            return str(js["_decrypt_error"])
    preview = (raw_text or "").strip().replace("\n", " ")[:180]
    if preview:
        return f"HTTP {http_status}: {preview}"
    return f"HTTP {http_status}"


def fetch_mission_reward(
    session: dict, mission_id: int, level_id: int
) -> dict[str, Any]:
    """
    POST /server/mission/reward — JSON thường (không mã hóa), giống curl browser.
    Body: {"mission_id": 17, "level_id": 114}
    """
    plain = {"mission_id": int(mission_id), "level_id": int(level_id)}
    form_token = get_form_token(session)
    headers = build_common_headers(
        session,
        form_token=form_token,
        content_type="application/x-www-form-urlencoded/json",
    )
    url = f"{BASE_URL}{MISSION_REWARD_PATH}"
    body = json.dumps(plain, separators=(",", ":"))
    r = _game_http(session).post(url, data=body, headers=headers, timeout=35)
    apply_response_tokens(session, r.headers)
    _merge_response_cookies(session, r)
    raw_text = r.text or ""
    try:
        js: Any = r.json()
    except Exception:
        return {
            "ok": False,
            "http_status": r.status_code,
            "code": None,
            "msg": _reward_error_msg(r.status_code, None, raw_text),
            "mission_id": mission_id,
            "level_id": level_id,
            "raw": raw_text[:500],
        }
    ok = r.status_code == 200 and isinstance(js, dict) and js.get("code") == 1
    msg = js.get("msg") if isinstance(js, dict) else None
    if not msg and not ok:
        msg = _reward_error_msg(r.status_code, js, raw_text)
    return {
        "ok": ok,
        "http_status": r.status_code,
        "code": js.get("code") if isinstance(js, dict) else None,
        "msg": msg,
        "mission_id": mission_id,
        "level_id": level_id,
        "raw": js,
    }


def _sign_id(item: dict[str, Any]) -> int | None:
    for k in ("id", "sign_id", "signId"):
        v = item.get(k)
        if v is None or v == "":
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return None


def _done_bet_money(item: dict[str, Any]) -> int:
    """Chỉ đọc done_bet_money — không dùng bet_money (mốc yêu cầu)."""
    for k in ("done_bet_money", "doneBetMoney"):
        v = item.get(k)
        if v is None or v == "":
            continue
        try:
            return int(round(float(v)))
        except (TypeError, ValueError):
            continue
    return 0


def _level_bet_target(item: dict[str, Any]) -> int:
    v = item.get("bet_money") or item.get("betMoney")
    try:
        return int(round(float(v or 0)))
    except (TypeError, ValueError):
        return 0


def _level_list(item: dict[str, Any]) -> list[dict[str, Any]]:
    levels = item.get("levelList") or item.get("level_list") or []
    if not isinstance(levels, list):
        return []
    return [lv for lv in levels if isinstance(lv, dict)]


def _status_label(st: int | None) -> str:
    if st is None:
        return "—"
    if st == 0:
        return "Chưa đủ ĐK"
    if st == 1:
        return "Được nhận"
    if st == 2:
        return "Đã nhận"
    return f"st={st}"


def _level_row(lv: dict[str, Any], *, mission_id: int) -> dict[str, Any]:
    """Một phần tử levelList — status 0/1/2 từ API."""
    raw_st = lv.get("status")
    st: int | None
    try:
        st = int(raw_st) if raw_st is not None and raw_st != "" else None
    except (TypeError, ValueError):
        st = None
    lid = lv.get("id")
    try:
        lid = int(lid) if lid is not None and lid != "" else None
    except (TypeError, ValueError):
        lid = None
    return {
        "id": lid,
        "level_id": lid,
        "mission_id": mission_id,
        "status": st,
        "status_label": _status_label(st),
        "title": str(lv.get("title") or ""),
        "level": int(lv.get("level") or 0),
        "progress": int(lv.get("progress") or 0),
        "done_bet_money": _done_bet_money(lv),
        "bet_target": _level_bet_target(lv),
        "done_bet_count": int(lv.get("done_bet_count") or 0),
        "claimable": st == REWARD_CLAIM_STATUS,
    }


def _empty_level_row(level_id: int, mission_id: int) -> dict[str, Any]:
    return {
        "id": level_id,
        "level_id": level_id,
        "mission_id": mission_id,
        "status": None,
        "status_label": "—",
        "title": "",
        "level": 0,
        "progress": 0,
        "done_bet_money": 0,
        "bet_target": 0,
        "done_bet_count": 0,
        "claimable": False,
    }


def _levels_by_id(item: dict[str, Any], *, mission_id: int) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for lv in _level_list(item):
        row = _level_row(lv, mission_id=mission_id)
        lid = row.get("id")
        if lid is not None:
            out[int(lid)] = row
    return out


def parse_sign17_week_levels(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Sign 17 — luôn trả đủ level id 114–120."""
    by_id = _levels_by_id(item, mission_id=SIGN_ID_MINI_WEEK)
    return [
        by_id.get(lid) or _empty_level_row(lid, SIGN_ID_MINI_WEEK)
        for lid in LEVEL_IDS_WEEK
    ]


def _sign17_from_data(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return parse_sign17_week_levels({})
    for item in data.get("sign_list") or data.get("signList") or []:
        if isinstance(item, dict) and _sign_id(item) == SIGN_ID_MINI_WEEK:
            return parse_sign17_week_levels(item)
    return parse_sign17_week_levels({})


def format_level_ids_status(levels: list[dict[str, Any]], ids: tuple[int, ...]) -> str:
    by_id = {int(x["id"]): x for x in levels if x.get("id") is not None}
    parts: list[str] = []
    for lid in ids:
        row = by_id.get(lid)
        if row is None or row.get("status") is None:
            parts.append(f"id={lid} —")
        else:
            parts.append(f"id={lid} status={row['status']}")
    return ", ".join(parts)


def parse_sign22_daily(item: dict[str, Any]) -> dict[str, Any]:
    """Sign 22 — levelList (level_id 161)."""
    by_id = _levels_by_id(item, mission_id=SIGN_ID_DAILY_MISSION)
    row = by_id.get(DAILY_LEVEL_ID) or _empty_level_row(
        DAILY_LEVEL_ID, SIGN_ID_DAILY_MISSION
    )
    level_rows = [_level_row(lv, mission_id=SIGN_ID_DAILY_MISSION) for lv in _level_list(item)]
    if not any(x.get("id") == DAILY_LEVEL_ID for x in level_rows):
        level_rows = [row]
    return {
        "status": row.get("status") if row.get("status") is not None else -1,
        "status_label": row["status_label"],
        "level_id": DAILY_LEVEL_ID,
        "level_title": row["title"],
        "progress": row["progress"],
        "done_bet_money": row["done_bet_money"],
        "bet_target": row["bet_target"],
        "done_bet_count": row["done_bet_count"],
        "level_list": level_rows,
        "daily_row": row,
    }


def collect_tracked_levels(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """114–120 (mission 17) + 161 (mission 22)."""
    week = _sign17_from_data(data)
    daily = (_sign22_from_data(data).get("daily_row")) or _empty_level_row(
        DAILY_LEVEL_ID, SIGN_ID_DAILY_MISSION
    )
    return list(week) + [daily]


def parse_sign_mission_rows(
    data: dict[str, Any] | None,
    *,
    sign_ids: tuple[int, ...] = TRACK_SIGN_IDS,
) -> list[dict[str, Any]]:
    """Một dòng / sign trong sign_list (mức đang chạy + done_bet_money)."""
    if not isinstance(data, dict):
        return []

    sign_list = data.get("sign_list") or data.get("signList") or []
    if not isinstance(sign_list, list):
        return []

    rows: list[dict[str, Any]] = []
    for item in sign_list:
        if not isinstance(item, dict):
            continue
        sid = _sign_id(item)
        if sid is None or sid not in sign_ids:
            continue
        if sid == SIGN_ID_MINI_WEEK:
            week = parse_sign17_week_levels(item)
            rows.append(
                {
                    "sign_id": sid,
                    "title": str(item.get("title") or ""),
                    "type_format": str(item.get("type_format") or ""),
                    "is_minigame": int(item.get("is_minigame") or 0),
                    "level_list": week,
                }
            )
            continue
        d = parse_sign22_daily(item)
        rows.append(
            {
                "sign_id": sid,
                "title": str(item.get("title") or ""),
                "type_format": str(item.get("type_format") or ""),
                "is_minigame": int(item.get("is_minigame") or 0),
                "status": d["status"],
                "status_label": d["status_label"],
                "level_id": d.get("level_id"),
                "done_bet_money": d["done_bet_money"],
                "progress": d["progress"],
                "level_title": d["level_title"],
                "bet_target": d["bet_target"],
                "done_bet_count": d.get("done_bet_count", 0),
                "level_list": d.get("level_list") or [],
            }
        )
    return rows


def parse_sign_daily_bets(
    data: dict[str, Any] | None,
    *,
    sign_ids: tuple[int, ...] = TRACK_SIGN_IDS,
) -> dict[int, int]:
    """Trích done_bet_money theo sign_list id (mặc định 22)."""
    rows = parse_sign_mission_rows(data, sign_ids=sign_ids)
    return {int(r["sign_id"]): int(r["done_bet_money"]) for r in rows}


def _claim_between_delay_sec() -> float:
    return float(os.environ.get("XOSO66_MISSION_CLAIM_DELAY_SEC", "8"))


def mission_claim_display_name(lv: dict[str, Any]) -> str:
    """Tên đọc được cho log nhận thưởng (không dùng level_id 161)."""
    lid = int(lv.get("level_id") or lv.get("id") or 0)
    mid = int(lv.get("mission_id") or LEVEL_MISSION_MAP.get(lid, 0))
    if lid == DAILY_LEVEL_ID or mid == SIGN_ID_DAILY_MISSION:
        return "Điểm Danh Ngày"
    if lid in LEVEL_IDS_WEEK or mid == SIGN_ID_MINI_WEEK:
        day = int(lv.get("level") or 0)
        if day < 1:
            try:
                day = LEVEL_IDS_WEEK.index(lid) + 1
            except ValueError:
                day = max(1, lid - 113)
        return f"MiniGame Điểm Danh Ngày {day}"
    title = str(lv.get("title") or "").strip()
    if title and "cửa" in title.lower():
        return f"MINI GAME {title}"
    if title:
        return f"Nhiệm vụ {title}"
    return f"Nhiệm vụ (level {lid})"


def _print_mission_claim_result(
    *,
    log_prefix: str,
    username: str,
    display_name: str,
    ok: bool,
    msg: str,
) -> None:
    p = str(log_prefix or "").strip()
    u = str(username or "").strip()
    if p:
        if ok:
            print(f"{p} {u} Nhận {display_name} Thành Công", flush=True)
        else:
            err = str(msg or "lỗi").strip()
            print(f"{p} {u} Nhận {display_name} : {err}", flush=True)
        return
    tag = "OK" if ok else "FAIL"
    print(f"      {tag}: {msg}", flush=True)


def execute_mission_claims(
    session: dict,
    account_id: str,
    username: str,
    claimable: list[dict[str, Any]],
    *,
    account_proxy: str = "",
    log_prefix: str = "",
) -> list[dict[str, Any]]:
    """POST reward cho mọi level status=1; persist session sau mỗi lần."""
    from xoso66_proxy import proxy_source_label

    u = str(username or "").strip()
    aid = str(account_id).strip()
    claims: list[dict[str, Any]] = []
    if not claimable:
        return claims
    between_delay = _claim_between_delay_sec()
    auto_log = bool(str(log_prefix or "").strip())
    if not auto_log:
        print(f"  [{u}] nhận thưởng {len(claimable)} mức (status=1) ...", flush=True)
    if between_delay > 0 and claimable:
        time.sleep(between_delay)
    for i, lv in enumerate(claimable):
        mid = int(lv["mission_id"])
        lid = int(lv["level_id"])
        display = mission_claim_display_name(lv)
        if not auto_log:
            print(
                f"      POST reward mission_id={mid} level_id={lid} "
                f"({proxy_source_label(session, account_proxy=account_proxy)}) ...",
                flush=True,
            )
        cr = fetch_mission_reward(session, mid, lid)
        claims.append(cr)
        msg = str(cr.get("msg") or "")
        from xoso66_account_errors import maybe_mark_account_loi

        if maybe_mark_account_loi(aid, msg, source="mission/reward"):
            break
        _print_mission_claim_result(
            log_prefix=log_prefix,
            username=u,
            display_name=display,
            ok=bool(cr.get("ok")),
            msg=msg,
        )
        if not auto_log and not cr.get("ok") and "IP" in msg and "nhận" in msg.lower():
            print(
                "      → Site giới hạn theo IP outbound (proxy), không theo username. "
                "Proxy này có thể đã nhận Điểm Danh Ngày hôm nay (acc khác / tay trên IP khác). "
                "Gán proxy IP riêng cho từng acc hoặc nhận trên browser đúng IP acc.",
                flush=True,
            )
        persist_session(aid, session)
        if i < len(claimable) - 1 and between_delay > 0:
            time.sleep(between_delay)
    return claims


def process_username(
    username: str,
    *,
    force_login: bool = False,
    do_claim: bool = True,
) -> dict[str, Any]:
    u = str(username or "").strip()
    row = get_account_by_username(u)
    if not row:
        return {
            "ok": False,
            "username": u,
            "error": f"Không tìm thấy acc username={u!r} trong DB",
        }
    aid = str(row["id"])
    row_proxy = str(row.get("proxy") or "").strip()
    try:
        print(f"  [{u}] session/login ...", flush=True)
        session = ensure_session(aid, force_login=force_login)
    except Exception as e:
        return {"ok": False, "username": u, "account_id": aid, "error": str(e)}

    from xoso66_proxy import proxy_source_label

    proxy_line = proxy_source_label(session, account_proxy=row_proxy)
    print(f"  [{u}] {proxy_line}", flush=True)
    if not row_proxy:
        print(
            f"  [{u}] CẢNH BÁO: acc không có proxy trong DB — "
            "mọi API (list/reward) dùng default_proxy; nhiều acc → cùng IP → "
            "'IP này đã nhận thưởng'.",
            flush=True,
        )

    print(f"  [{u}] gọi mission/list ...", flush=True)
    rep = fetch_mission_list(session)
    persist_session(aid, session)

    if not rep.get("ok"):
        err = str(rep.get("msg") or rep.get("raw") or "mission/list thất bại")
        return {
            "ok": False,
            "username": u,
            "account_id": aid,
            "error": err,
            "http_status": rep.get("http_status"),
            "api_code": rep.get("code"),
        }

    data = rep.get("data")
    levels = collect_tracked_levels(data)

    from xoso66_mission_db import format_db_save_line, persist_mission_state

    mission_snap = persist_mission_state(u, aid, levels, phase="list")
    print(f"  [{u}] {format_db_save_line(mission_snap)}", flush=True)

    claimable = [x for x in levels if x.get("status") == REWARD_CLAIM_STATUS]
    claims: list[dict[str, Any]] = []

    if claimable and do_claim:
        claims = execute_mission_claims(
            session, aid, u, claimable, account_proxy=row_proxy
        )
        print(f"  [{u}] mission/list (sau nhận) ...", flush=True)
        rep2 = fetch_mission_list(session)
        persist_session(aid, session)
        if rep2.get("ok"):
            data = rep2.get("data")
            levels = collect_tracked_levels(data)
            mission_snap = persist_mission_state(u, aid, levels, phase="after_claim")
            print(f"  [{u}] {format_db_save_line(mission_snap)}", flush=True)
    elif claimable:
        print(f"  [{u}] {len(claimable)} mức status=1 (--check-only, bỏ qua reward)", flush=True)

    d22 = _sign22_from_data(data)
    week_levels = _sign17_from_data(data)
    claimed_ok = sum(1 for c in claims if c.get("ok"))

    print(
        f"  [{u}] Tóm tắt: Điểm danh status={mission_snap.get('daily_status')} | "
        f"MINI ngày {mission_snap.get('mini_current_day')} "
        f"(level_id={mission_snap.get('mini_current_level_id')}, "
        f"status={mission_snap.get('mini_current_status')})",
        flush=True,
    )

    return {
        "ok": True,
        "username": u,
        "account_id": aid,
        "levels": levels,
        "claimable": claimable,
        "claims": claims,
        "claims_ok": claimed_ok,
        "mission_db": mission_snap,
        "sign_17_levels": week_levels,
        "sign_22_level_list": d22.get("level_list") or [],
        "sign_rows": parse_sign_mission_rows(data),
        "sign_list_tracked": extract_tracked_sign_items(data),
    }


def check_username(username: str, *, force_login: bool = False) -> dict[str, Any]:
    """Alias — mặc định có nhận thưởng."""
    return process_username(username, force_login=force_login, do_claim=True)


def _sign22_from_data(data: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(data, dict):
        return parse_sign22_daily({})
    for item in data.get("sign_list") or data.get("signList") or []:
        if isinstance(item, dict) and _sign_id(item) == SIGN_ID_DAILY_MISSION:
            return parse_sign22_daily(item)
    return parse_sign22_daily({})


def _prompt_usernames_interactive() -> list[str]:
    """Hỏi username khi chạy không truyền tham số."""
    try:
        line = input(
            "Username (nhiều user cách nhau bởi dấu cách, Enter trống = thoát): "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print(flush=True)
        return []
    if not line:
        return []
    return [p for p in line.replace(",", " ").split() if p.strip()]


def _read_usernames_file(path: Path) -> list[str]:
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        lines.append(s.split()[0])
    return lines


def _collect_usernames(args: argparse.Namespace) -> list[str]:
    names: list[str] = []
    if args.file:
        names.extend(_read_usernames_file(Path(args.file)))
    names.extend(args.usernames or [])
    if args.all_accounts:
        init_db()
        rows = list_accounts()
        names.extend(str(r.get("username") or "") for r in rows)
    if args.status:
        init_db()
        for r in list_accounts_by_status(args.status):
            names.extend([str(r.get("username") or "")])
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        k = n.strip().lower()
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(n.strip())
    return out


def extract_tracked_sign_items(
    data: dict[str, Any] | None,
    *,
    sign_ids: tuple[int, ...] = TRACK_SIGN_IDS,
) -> list[dict[str, Any]]:
    """Nguyên bản phần tử sign_list id 17 & 22 để in / debug."""
    if not isinstance(data, dict):
        return []
    sign_list = data.get("sign_list") or data.get("signList") or []
    if not isinstance(sign_list, list):
        return []
    want = set(int(x) for x in sign_ids)
    out: list[dict[str, Any]] = []
    for item in sign_list:
        if not isinstance(item, dict):
            continue
        sid = _sign_id(item)
        if sid is not None and sid in want:
            out.append(item)
    return out


def _print_sign_list_block(username: str, rows: list[dict[str, Any]], raw_items: list[dict]) -> None:
    print(f"\n--- sign_list [{username}] ---", flush=True)
    for r in rows:
        sid = r.get("sign_id")
        print(
            f"  [sign {sid}] {r.get('title')} ({r.get('type_format')}) "
            f"minigame={r.get('is_minigame')}",
            flush=True,
        )
        for lv in r.get("level_list") or []:
            st = lv.get("status")
            st_s = "—" if st is None else str(st)
            print(
                f"      levelList id={lv.get('id')} \"{lv.get('title')}\" | "
                f"status={st_s} | progress={lv.get('progress')}% | "
                f"done_bet_money={int(lv.get('done_bet_money') or 0):,}",
                flush=True,
            )
    if raw_items:
        print("  (JSON sign_list id 17 & 22):", flush=True)
        print(json.dumps(raw_items, ensure_ascii=False, indent=2), flush=True)


def _print_table(rows: list[dict[str, Any]], *, show_signs: bool = False) -> None:
    hdr_user = "username"
    w_user = max(
        [len(hdr_user)]
        + [len(str(r.get("username") or "")) for r in rows]
        or [8]
    )
    id_cols = [str(i) for i in LEVEL_IDS_WEEK]
    w_id = max(6, max(len(c) for c in id_cols))
    col_22 = "sign22 (161)"
    w22 = len(col_22)
    hdr = (
        f"{hdr_user.ljust(w_user)}  "
        + "  ".join(c.rjust(w_id) for c in id_cols)
        + f"  {col_22.rjust(w22)}"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        u = str(r.get("username") or "")
        if not r.get("ok"):
            note = str(r.get("error") or "lỗi")[:80]
            print(f"{u.ljust(w_user)}  LỖI: {note}")
            continue
        by_id = {
            int(x["id"]): x
            for x in (r.get("sign_17_levels") or [])
            if x.get("id") is not None
        }
        cells: list[str] = []
        for lid in LEVEL_IDS_WEEK:
            row = by_id.get(lid)
            if row is None or row.get("status") is None:
                cells.append("—".rjust(w_id))
            else:
                cells.append(str(row["status"]).rjust(w_id))
        s22 = r.get("sign_22_level_list") or []
        s22_txt = "—"
        if s22:
            parts = [f"{x.get('id')}:{x.get('status')}" for x in s22 if x.get("id") is not None]
            s22_txt = " ".join(parts) if parts else "—"
        print(f"{u.ljust(w_user)}  " + "  ".join(cells) + f"  {s22_txt.rjust(w22)}")
    print(
        "\n  status: 0=chua du DK, 1=duoc nhan (goi reward), 2=da nhan. "
        "114-120 -> mission 17; 161 -> mission 22.",
        flush=True,
    )
    for r in rows:
        if not r.get("ok"):
            continue
        claims = r.get("claims") or []
        if not claims:
            continue
        u = r.get("username")
        print(f"\n  [{u}] ket qua reward:", flush=True)
        for c in claims:
            tag = "OK" if c.get("ok") else "FAIL"
            print(
                f"    {tag} mission_id={c.get('mission_id')} level_id={c.get('level_id')}: "
                f"{c.get('msg')}",
                flush=True,
            )
    if show_signs:
        for r in rows:
            if not r.get("ok"):
                continue
            u = str(r.get("username") or "")
            _print_sign_list_block(
                u,
                r.get("sign_rows") or [],
                r.get("sign_list_tracked") or [],
            )


def refresh_missions_batch(
    *,
    account_ids: list[str] | None = None,
    status_filter: str | None = "Đang Chơi",
    check_only: bool = True,
    force_login: bool = False,
    parallel: int = 8,
) -> dict[str, Any]:
    """
    CMS Refresh: gọi mission/list → lưu DB (161 + MINI) cho nhiều acc.
    check_only=True: không nhận thưởng (chỉ cập nhật trạng thái).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from xoso66_accounts_db import get_account, list_accounts, list_accounts_by_status

    init_db()
    crypto_err = _require_crypto()
    if crypto_err:
        return {"ok": False, "error": crypto_err, "results": []}

    names: list[str] = []
    if account_ids:
        for aid in account_ids:
            row = get_account(str(aid).strip())
            if row:
                u = str(row.get("username") or "").strip()
                if u:
                    names.append(u)
    elif status_filter and str(status_filter).strip():
        for row in list_accounts_by_status(str(status_filter).strip()):
            u = str(row.get("username") or "").strip()
            if u:
                names.append(u)
    else:
        for row in list_accounts():
            u = str(row.get("username") or "").strip()
            if u:
                names.append(u)

    seen: set[str] = set()
    unique: list[str] = []
    for u in names:
        k = u.lower()
        if k not in seen:
            seen.add(k)
            unique.append(u)

    mode = "chỉ xem" if check_only else "check + nhận thưởng"
    scope = f"{len(unique)} nick"
    if status_filter and not account_ids:
        scope += f' (status="{status_filter}")'
    elif account_ids:
        scope += " (theo account_ids)"
    print(
        f"[MISSION-CMS] Bắt đầu {scope} — {mode}, song song {max(1, min(32, int(parallel or 8)))}",
        flush=True,
    )

    t0 = time.time()
    results: list[dict[str, Any]] = []
    workers = max(1, min(32, int(parallel or 8)))

    def _one(username: str) -> dict[str, Any]:
        return process_username(
            username,
            force_login=force_login,
            do_claim=not check_only,
        )

    if workers <= 1 or len(unique) <= 1:
        for u in unique:
            results.append(_one(u))
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_one, u): u for u in unique}
            for fut in as_completed(futs):
                try:
                    results.append(fut.result())
                except Exception as e:
                    results.append(
                        {
                            "ok": False,
                            "username": futs[fut],
                            "error": str(e),
                        }
                    )

    ok_n = sum(1 for r in results if r.get("ok"))
    fail_n = len(results) - ok_n
    claimed_n = sum(int(r.get("claims_ok") or 0) for r in results)
    print(
        f"[MISSION-CMS] Xong {scope} — OK {ok_n}/{len(results)}"
        + (f", lỗi {fail_n}" if fail_n else "")
        + (f", đã nhận {claimed_n} thưởng" if claimed_n else "")
        + f" ({round(time.time() - t0, 1)}s)",
        flush=True,
    )
    return {
        "ok": fail_n == 0,
        "total": len(results),
        "ok_count": ok_n,
        "fail_count": fail_n,
        "check_only": bool(check_only),
        "status_filter": status_filter or "",
        "elapsed_sec": round(time.time() - t0, 1),
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="mission/list + tu nhan reward (status=1) cho level 114-120, 161."
    )
    ap.add_argument("usernames", nargs="*", help="username hoặc account id (acc10)")
    ap.add_argument(
        "--file",
        "-f",
        help="file mỗi dòng 1 username (bỏ qua dòng #)",
    )
    ap.add_argument("--all", dest="all_accounts", action="store_true", help="mọi acc trong DB")
    ap.add_argument("--status", help='lọc status CMS, vd. "Đang Chơi"')
    ap.add_argument("--json", action="store_true", help="in JSON")
    ap.add_argument(
        "--signs",
        action="store_true",
        help="in chi tiết sign_list (level đang chạy, done_bet_money)",
    )
    ap.add_argument("--login", action="store_true", help="ép login lại trước khi gọi API")
    ap.add_argument(
        "--check-only",
        action="store_true",
        help="chỉ xem status, không gọi /server/mission/reward",
    )
    args = ap.parse_args(argv)

    init_db()
    crypto_err = _require_crypto()
    if crypto_err:
        print(crypto_err, flush=True)
        return 1

    names = _collect_usernames(args)
    if not names:
        if not sys.stdin.isatty():
            for line in sys.stdin:
                s = line.strip()
                if s and not s.startswith("#"):
                    names.append(s.split()[0])
        elif sys.stdin.isatty():
            names = _prompt_usernames_interactive()
        if not names:
            return 0

    print(f"\nKiểm tra {len(names)} tài khoản (có thể mất vài phút nếu phải login)...\n", flush=True)
    results: list[dict[str, Any]] = []
    for idx, name in enumerate(names, 1):
        print(f"[{idx}/{len(names)}] {name}", flush=True)
        row = get_account_by_username(name)
        if row:
            uname = str(row.get("username") or name)
        else:
            from xoso66_accounts_db import get_account

            by_id = get_account(name)
            if by_id:
                uname = str(by_id.get("username") or name)
                name = uname
            else:
                results.append(
                    {
                        "ok": False,
                        "username": name,
                        "error": "không có trong DB (dùng username hoặc acc id)",
                    }
                )
                continue
        row_result = process_username(
            uname,
            force_login=args.login,
            do_claim=not args.check_only,
        )
        if not row_result.get("ok"):
            print(f"  Lỗi: {row_result.get('error')}", flush=True)
        results.append(row_result)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        _print_table(results, show_signs=bool(args.signs))
        ok_n = sum(1 for r in results if r.get("ok"))
        print(f"\nOK {ok_n}/{len(results)}", flush=True)

    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
