# -*- coding: utf-8 -*-
"""
Nhận thưởng nhiệm vụ MINI GAME (tab «Nhiệm vụ») — mission/list.mission_list + mission/reward.

Khác với xoso66_daily_mission_check.py (sign_list: điểm danh 161 + MINI 7 ngày).
Script này chỉ xử lý mission_list → MINI GAME «Cửa thứ N».

Chạy:
  python xoso66_task_mission_reward.py hondatbxm
  python xoso66_task_mission_reward.py all
  python xoso66_task_mission_reward.py hondatbxm --check-only
  python xoso66_task_mission_reward.py all --login
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from xoso66_accounts_db import (
    get_account,
    get_account_by_username,
    init_db,
    list_accounts,
    list_accounts_by_status,
)
from xoso66_config_util import configure_stdio_utf8, safe_print
from xoso66_daily_mission_check import (
    REWARD_CLAIM_STATUS,
    _level_row,
    execute_mission_claims,
    fetch_mission_list,
)
from xoso66_session import ensure_session, persist_session

configure_stdio_utf8()
print = safe_print  # noqa: A001


def _mission_list_from_data(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    raw = data.get("mission_list") or data.get("missionList") or []
    if not isinstance(raw, list):
        return []
    return [x for x in raw if isinstance(x, dict)]


def _mission_id(item: dict[str, Any]) -> int | None:
    for k in ("id", "mission_id", "missionId"):
        v = item.get(k)
        if v is None or v == "":
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return None


def find_minigame_mission(mission_list: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Chọn mission MINI GAME trong mission_list (tab Nhiệm vụ)."""
    for item in mission_list:
        title = str(item.get("title") or "").strip().upper()
        if "MINI GAME" in title:
            return item
    for item in mission_list:
        if int(item.get("is_minigame") or 0) == 1:
            fmt = str(item.get("type_format") or "").strip()
            if fmt == "Qua cửa":
                return item
    return None


def parse_task_missions(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Trả về mission_list đã chuẩn hóa (mỗi phần tử có mission_id)."""
    out: list[dict[str, Any]] = []
    for item in _mission_list_from_data(data):
        mid = _mission_id(item)
        if mid is None:
            continue
        row = dict(item)
        row["mission_id"] = mid
        out.append(row)
    return out


def collect_task_levels(
    mission_item: dict[str, Any],
) -> list[dict[str, Any]]:
    """Mọi level trong mission_list item — kèm mission_id."""
    mid = int(mission_item.get("mission_id") or _mission_id(mission_item) or 0)
    levels = mission_item.get("levelList") or mission_item.get("level_list") or []
    if not isinstance(levels, list):
        return []
    rows: list[dict[str, Any]] = []
    for lv in levels:
        if not isinstance(lv, dict):
            continue
        row = _level_row(lv, mission_id=mid)
        row["prize"] = int(lv.get("prize") or 0)
        rows.append(row)
    return rows


# Cửa thứ 1 Nhiệm vụ MINI GAME — target cược (site). Cap gợi ý ≈ 2_695_000.
TASK_CUA1_BET_TARGET_VND = 2_688_000


def collect_claimable_task_levels(
    mission_item: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        x
        for x in collect_task_levels(mission_item)
        if x.get("status") == REWARD_CLAIM_STATUS
    ]


def collect_claimable_task_levels_for_cap(
    data: dict[str, Any] | None,
    cap_vnd: int,
) -> list[dict[str, Any]]:
    """
    Cửa status=1 có bet_target <= daily_bet_cap (bỏ cửa cao hơn cap đang chạy).
    bet_target<=0: vẫn nhận nếu status=1 (API thiếu target).
    """
    mission = find_minigame_mission(parse_task_missions(data))
    if not mission:
        return []
    cap = max(0, int(cap_vnd or 0))
    out: list[dict[str, Any]] = []
    for lv in collect_claimable_task_levels(mission):
        target = int(lv.get("bet_target") or 0)
        if target > 0 and target > cap:
            continue
        out.append(lv)
    return out


def collect_task_levels_from_data(
    data: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    mission = find_minigame_mission(parse_task_missions(data))
    return collect_task_levels(mission) if mission else []


def needs_task_cua_bet_poll(
    task_levels: list[dict[str, Any]],
    daily_bet_total: int | float,
    cap_vnd: int,
) -> tuple[bool, str]:
    """
    Poll chờ site mở cửa: daily >= bet_target <= cap nhưng status vẫn 0.
    Trả (cần_poll, mô_tả).
    """
    daily = int(daily_bet_total or 0)
    cap = max(0, int(cap_vnd or 0))
    if daily <= 0 or cap <= 0:
        return False, ""
    pending: list[str] = []
    for lv in task_levels or []:
        target = int(lv.get("bet_target") or 0)
        if target <= 0 or target > cap:
            continue
        if daily < target:
            continue
        st = lv.get("status")
        try:
            st_i = int(st) if st is not None and st != "" else None
        except (TypeError, ValueError):
            st_i = None
        if st_i == 0:
            title = str(lv.get("title") or f"level {lv.get('level_id')}").strip()
            pending.append(f"{title} {daily:,}/{target:,}")
    if not pending:
        return False, ""
    return True, "; ".join(pending[:3])


def task_level_display_name(lv: dict[str, Any], *, mission_title: str = "") -> str:
    title = str(lv.get("title") or "").strip()
    if title:
        return f"MINI GAME {title}"
    lid = int(lv.get("level_id") or lv.get("id") or 0)
    mt = str(mission_title or "MINI GAME").strip()
    return f"{mt} (level {lid})"


def _print_mission_block(
    username: str,
    mission: dict[str, Any] | None,
    levels: list[dict[str, Any]],
) -> None:
    if not mission:
        print(f"  [{username}] Không tìm thấy mission MINI GAME trong mission_list", flush=True)
        return
    mid = mission.get("mission_id") or _mission_id(mission)
    print(
        f"  [{username}] mission_id={mid} «{mission.get('title')}» "
        f"({mission.get('type_format')})",
        flush=True,
    )
    for lv in levels:
        st = lv.get("status")
        st_s = "—" if st is None else str(st)
        print(
            f"      level_id={lv.get('level_id')} \"{lv.get('title')}\" | "
            f"status={st_s} | prize={int(lv.get('prize') or 0):,} | "
            f"done={int(lv.get('done_bet_money') or 0):,}/"
            f"{int(lv.get('bet_target') or 0):,}",
            flush=True,
        )


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

    print(f"  [{u}] {proxy_source_label(session, account_proxy=row_proxy)}", flush=True)

    print(f"  [{u}] gọi mission/list (mission_list) ...", flush=True)
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
    mission = find_minigame_mission(parse_task_missions(data))
    levels = collect_task_levels(mission) if mission else []
    claimable = collect_claimable_task_levels(mission) if mission else []
    claims: list[dict[str, Any]] = []

    _print_mission_block(u, mission, levels)

    if claimable and do_claim:
        claim_rows = []
        mt = str((mission or {}).get("title") or "MINI GAME")
        for lv in claimable:
            row_lv = dict(lv)
            row_lv["_display"] = task_level_display_name(lv, mission_title=mt)
            claim_rows.append(row_lv)
        print(f"  [{u}] nhận thưởng {len(claimable)} mức MINI GAME ...", flush=True)
        claims = execute_mission_claims(
            session,
            aid,
            u,
            claimable,
            account_proxy=row_proxy,
        )
        print(f"  [{u}] mission/list (sau nhận) ...", flush=True)
        rep2 = fetch_mission_list(session)
        persist_session(aid, session)
        if rep2.get("ok"):
            data = rep2.get("data")
            mission = find_minigame_mission(parse_task_missions(data))
            levels = collect_task_levels(mission) if mission else []
            claimable = collect_claimable_task_levels(mission) if mission else []
            _print_mission_block(u, mission, levels)
    elif claimable:
        print(
            f"  [{u}] {len(claimable)} mức status=1 (--check-only, bỏ qua reward)",
            flush=True,
        )
    else:
        print(f"  [{u}] Không có thưởng MINI GAME chờ nhận (status=1)", flush=True)

    claimed_ok = sum(1 for c in claims if c.get("ok"))
    return {
        "ok": True,
        "username": u,
        "account_id": aid,
        "mission": mission,
        "levels": levels,
        "claimable": claimable,
        "claims": claims,
        "claims_ok": claimed_ok,
    }


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
    for n in args.usernames or []:
        if str(n).strip().lower() == "all":
            args.all_accounts = True
        else:
            names.append(str(n).strip())
    if args.all_accounts:
        init_db()
        rows = list_accounts()
        names.extend(str(r.get("username") or "") for r in rows)
    if args.status:
        init_db()
        for r in list_accounts_by_status(args.status):
            names.append(str(r.get("username") or ""))
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        k = n.strip().lower()
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(n.strip())
    return out


def _print_summary(rows: list[dict[str, Any]]) -> None:
    hdr = f"{'username':<16}  {'claimable':>9}  {'claimed_ok':>10}  note"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        u = str(r.get("username") or "")
        if not r.get("ok"):
            print(f"{u:<16}  {'—':>9}  {'—':>10}  {str(r.get('error') or 'lỗi')[:60]}")
            continue
        cl = len(r.get("claimable") or [])
        ok = int(r.get("claims_ok") or 0)
        note = "OK"
        if cl and ok == 0 and not (r.get("claims") or []):
            note = f"{cl} chờ nhận (--check-only)"
        elif cl == 0:
            note = "không có thưởng"
        print(f"{u:<16}  {cl:>9}  {ok:>10}  {note}")
    print(
        "\n  status: 0=chưa đủ ĐK, 1=được nhận, 2=đã nhận. "
        "Tab Nhiệm vụ → MINI GAME (mission_list).",
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Nhận thưởng nhiệm vụ MINI GAME (mission_list) — status=1."
    )
    ap.add_argument(
        "usernames",
        nargs="*",
        help="username hoặc all (nhận hết mọi acc trong DB)",
    )
    ap.add_argument("--file", "-f", help="file mỗi dòng 1 username")
    ap.add_argument("--all", dest="all_accounts", action="store_true", help="mọi acc trong DB")
    ap.add_argument("--status", help='lọc status CMS, vd. "Đang Chơi"')
    ap.add_argument("--json", action="store_true", help="in JSON")
    ap.add_argument("--login", action="store_true", help="ép login lại")
    ap.add_argument(
        "--check-only",
        action="store_true",
        help="chỉ xem status, không gọi /server/mission/reward",
    )
    args = ap.parse_args(argv)

    init_db()
    names = _collect_usernames(args)
    if not names:
        print("Không có username — truyền: python xoso66_task_mission_reward.py <user>|all", flush=True)
        return 1

    print(f"\nNhiệm vụ MINI GAME — {len(names)} tài khoản\n", flush=True)
    results: list[dict[str, Any]] = []
    for idx, name in enumerate(names, 1):
        print(f"[{idx}/{len(names)}] {name}", flush=True)
        row = get_account_by_username(name)
        if row:
            uname = str(row.get("username") or name)
        else:
            by_id = get_account(name)
            if by_id:
                uname = str(by_id.get("username") or name)
            else:
                results.append(
                    {
                        "ok": False,
                        "username": name,
                        "error": "không có trong DB (dùng username hoặc acc id)",
                    }
                )
                print(f"  Lỗi: không có trong DB", flush=True)
                continue
        row_result = process_username(
            uname,
            force_login=args.login,
            do_claim=not args.check_only,
        )
        if not row_result.get("ok"):
            print(f"  Lỗi: {row_result.get('error')}", flush=True)
        results.append(row_result)
        print(flush=True)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        _print_summary(results)
        ok_n = sum(1 for r in results if r.get("ok"))
        claimed_n = sum(int(r.get("claims_ok") or 0) for r in results)
        tail = f"OK {ok_n}/{len(results)}"
        if claimed_n:
            tail += f", đã nhận {claimed_n} thưởng"
        print(f"\n{tail}", flush=True)

    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
