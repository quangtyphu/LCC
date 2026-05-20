# -*- coding: utf-8 -*-
"""
Kiểm tra + nhận thưởng VIP — vipList + activityreward.

Nhận thưởng: POST /server/activity/activityreward
  body: {"activity_id":1,"level_id":<id cấp VIP>,"reward_type":"upgrade"|"weekly"|"monthly"}

prize_list[].status (VIP reward — quan sát + suy luận, cập nhật 2026-05-19):
  0 — chưa đạt điều kiện (level/reward_type chưa mở).
  1 — được nhận ngay (gọi API claim khi implement).
  2 — đã nhận (đoán — chưa verify bằng thử nhận).
  3 — đủ ĐK nhưng chờ mốc thời gian (weekly: thứ 2 00:00, monthly: ngày 1 00:00);
      thường gặp trên reward_type weekly/monthly; đầu tuần/tháng có thể về 1.

Chạy (mặc định: check + tự nhận thưởng status=1 tại cấp VIP hiện tại):
  python xoso66_vip_check.py user1
  python xoso66_vip_check.py user1 user2
  python xoso66_vip_check.py              # hỏi username (interactive)
  python xoso66_vip_check.py user1 --check-only   # chỉ xem, không nhận
  python xoso66_vip_check.py user1 -q
  python xoso66_vip_check.py --status "Đang Chơi"

Response đầy đủ lưu vào thư mục --save-dir (kèm prize_status_legend).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from xoso66_accounts_db import (
    get_account,
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

VIP_LIST_PATH = "/server/activity/vipList"
VIP_ACTIVITY_REWARD_PATH = "/server/activity/activityreward"
VIP_ACTIVITY_ID = int(os.environ.get("XOSO66_VIP_ACTIVITY_ID", "1"))
DIR = Path(__file__).resolve().parent
DEFAULT_SAVE_DIR = DIR / "vip_check_outputs"

# prize_list[].status — lưu quy ước để claim / auto sau này
VIP_PRIZE_STATUS_NOT_ELIGIBLE = 0
VIP_PRIZE_STATUS_CLAIMABLE = 1
VIP_PRIZE_STATUS_CLAIMED = 2  # đoán — chưa verify
VIP_PRIZE_STATUS_WAITING_SCHEDULE = 3  # đoán: weekly/monthly chờ mốc ngày

VIP_PRIZE_STATUS_LEGEND: dict[int, str] = {
    0: "chua_dat_dk",
    1: "duoc_nhan",
    2: "da_nhan",  # chua verify
    3: "cho_moc_thoi_gian",  # weekly/monthly; dau tuan/thang co the -> 1
}

VIP_PRIZE_STATUS_NOTES: dict[int, str] = {
    0: "Chưa đạt điều kiện (level hoặc loại thưởng chưa mở).",
    1: "Được nhận — gọi API claim.",
    2: "Đã nhận (suy đoán, chưa thử nhận để xác nhận).",
    3: (
        "Đủ ĐK nhưng chờ mốc (thưởng tuần/tháng). "
        "Thường weekly reset thứ 2 00:00, monthly ngày 1 00:00 → có thể về status 1."
    ),
}


def prize_status_label(status: int | None) -> str:
    try:
        return VIP_PRIZE_STATUS_LEGEND.get(int(status or 0), f"unknown_{status}")
    except (TypeError, ValueError):
        return "unknown"


VIP_PRIZE_STATUS_VI: dict[int, str] = {
    0: "Chưa đạt",
    1: "Được nhận",
    2: "Đã nhận",
    3: "Chờ mốc (tuần/tháng)",
}

VIP_REWARD_TYPE_VI: dict[str, str] = {
    "upgrade": "Nâng cấp",
    "weekly": "Thưởng tuần",
    "monthly": "Thưởng tháng",
}


def prize_status_vi(status: int | None) -> str:
    try:
        return VIP_PRIZE_STATUS_VI.get(int(status or 0), f"?({status})")
    except (TypeError, ValueError):
        return "?"


def prize_nhan_chua_label(status: int | None) -> str:
    """Đã nhận / chưa nhận / chưa đạt / chờ mốc."""
    try:
        st = int(status or 0)
    except (TypeError, ValueError):
        return "—"
    if st == VIP_PRIZE_STATUS_CLAIMED:
        return "Đã nhận"
    if st == VIP_PRIZE_STATUS_CLAIMABLE:
        return "Chưa nhận"
    if st == VIP_PRIZE_STATUS_WAITING_SCHEDULE:
        return "Chưa (chờ mốc)"
    return "—"


def _fmt_vnd(amount: Any) -> str:
    try:
        return f"{int(amount):,}"
    except (TypeError, ValueError):
        return str(amount or "—")


def _require_crypto() -> str | None:
    try:
        from xoso66_deposit import crypto_available

        if crypto_available():
            return None
    except Exception:
        pass
    return "Thiếu pycryptodome — chạy: pip install pycryptodome"


def fetch_vip_list(session: dict) -> dict[str, Any]:
    """GET /server/activity/vipList — cookie + form-token + CF headers."""
    form_token = get_form_token(session)
    headers = build_common_headers(
        session,
        form_token=form_token,
        content_type="application/json",
    )
    url = f"{BASE_URL}{VIP_LIST_PATH}"
    r = _game_http(session).get(url, headers=headers, timeout=35)
    apply_response_tokens(session, r.headers)
    _merge_response_cookies(session, r)
    try:
        js = r.json()
    except Exception:
        return {
            "ok": False,
            "http_status": r.status_code,
            "raw_text": (r.text or "")[:2000],
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


def _save_payload(path: Path, payload: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _snapshot_path(save_dir: Path, username: str) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in username)
    return save_dir / f"{safe}_{ts}_vipList.json"


def _iter_prize_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten mọi prize_list kèm level + status_label."""
    rows: list[dict[str, Any]] = []
    levels = data.get("level_list") or data.get("levelList") or []
    if not isinstance(levels, list):
        return rows
    for lv in levels:
        if not isinstance(lv, dict):
            continue
        for p in lv.get("prize_list") or []:
            if not isinstance(p, dict):
                continue
            st = int(p.get("status") or 0)
            rows.append(
                {
                    "level": lv.get("level"),
                    "level_formatted": lv.get("level_formatted"),
                    "reward_type": p.get("reward_type"),
                    "title": p.get("title"),
                    "prize": p.get("prize"),
                    "status": st,
                    "status_label": prize_status_label(st),
                }
            )
    return rows


def _collect_prizes_by_status(
    data: dict[str, Any], status: int
) -> list[dict[str, Any]]:
    return [r for r in _iter_prize_rows(data) if r.get("status") == status]


def _collect_claimable_prizes(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Mọi cấp — status=1."""
    return _collect_prizes_by_status(data, VIP_PRIZE_STATUS_CLAIMABLE)


def _resolve_activity_id(data: dict[str, Any] | None) -> int:
    if isinstance(data, dict):
        try:
            raw = data.get("id") if data.get("id") is not None else data.get("activity_id")
            if raw is not None:
                return int(raw)
        except (TypeError, ValueError):
            pass
    return VIP_ACTIVITY_ID


def collect_claimable_current_level(
    data: dict[str, Any],
    *,
    activity_id: int | None = None,
    reward_types: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Thưởng status=1 tại cấp VIP hiện tại (để gọi activityreward)."""
    aid = int(activity_id if activity_id is not None else _resolve_activity_id(data))
    snap = extract_vip_snapshot(data)
    current = int(snap.get("current_level") or 0)
    lv_row = _find_level_row(data, current) if isinstance(data, dict) else None
    if not lv_row:
        return []
    try:
        level_id = int(lv_row.get("id") if lv_row.get("id") is not None else lv_row.get("level"))
    except (TypeError, ValueError):
        level_id = current
    want = {x.lower() for x in reward_types} if reward_types else None
    out: list[dict[str, Any]] = []
    for p in lv_row.get("prize_list") or []:
        if not isinstance(p, dict):
            continue
        if int(p.get("status") or 0) != VIP_PRIZE_STATUS_CLAIMABLE:
            continue
        rt = str(p.get("reward_type") or "").strip().lower()
        if want and rt not in want:
            continue
        out.append(
            {
                "activity_id": aid,
                "level_id": level_id,
                "reward_type": rt,
                "prize": int(p.get("prize") or 0),
                "title": p.get("title"),
                "level": current,
                "level_formatted": lv_row.get("level_formatted"),
            }
        )
    return out


def _reward_error_msg(http_status: int, js: Any, raw_text: str) -> str:
    if isinstance(js, dict):
        msg = js.get("msg")
        if msg:
            return str(msg)
        code = js.get("code")
        if code is not None:
            return f"code={code}"
    preview = (raw_text or "").strip().replace("\n", " ")[:180]
    if preview:
        return f"HTTP {http_status}: {preview}"
    return f"HTTP {http_status}"


def fetch_vip_activity_reward(
    session: dict,
    *,
    activity_id: int,
    level_id: int,
    reward_type: str,
) -> dict[str, Any]:
    """POST /server/activity/activityreward."""
    plain = {
        "activity_id": int(activity_id),
        "level_id": int(level_id),
        "reward_type": str(reward_type),
    }
    form_token = get_form_token(session)
    headers = build_common_headers(
        session,
        form_token=form_token,
        content_type="application/x-www-form-urlencoded/json",
    )
    url = f"{BASE_URL}{VIP_ACTIVITY_REWARD_PATH}"
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
            **plain,
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
        **plain,
        "raw": js,
    }


def execute_vip_claims(
    session: dict,
    account_id: str,
    username: str,
    claimable: list[dict[str, Any]],
    *,
    account_proxy: str = "",
    quiet: bool = False,
) -> list[dict[str, Any]]:
    from xoso66_proxy import proxy_source_label

    u = str(username or "").strip()
    aid = str(account_id).strip()
    claims: list[dict[str, Any]] = []
    if not claimable:
        return claims

    def _log(msg: str) -> None:
        if not quiet:
            print(msg, flush=True)

    _log(
        f"  [{u}] nhận {len(claimable)} thưởng VIP "
        f"({proxy_source_label(session, account_proxy=account_proxy)}) ..."
    )
    for item in claimable:
        rt = str(item.get("reward_type") or "")
        lid = int(item["level_id"])
        aid_act = int(item["activity_id"])
        prize = int(item.get("prize") or 0)
        loai = VIP_REWARD_TYPE_VI.get(rt, rt)
        _log(
            f"      POST activityreward {loai} level_id={lid} "
            f"prize={_fmt_vnd(prize)} ..."
        )
        cr = fetch_vip_activity_reward(
            session,
            activity_id=aid_act,
            level_id=lid,
            reward_type=rt,
        )
        claims.append({**cr, "loai": loai, "prize": prize})
        tag = "OK" if cr.get("ok") else "FAIL"
        _log(f"      {tag}: {cr.get('msg')}")
        persist_session(aid, session)
        time.sleep(0.35)
    return claims


def _prize_status_counts(data: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {label: 0 for label in VIP_PRIZE_STATUS_LEGEND.values()}
    for r in _iter_prize_rows(data):
        lbl = str(r.get("status_label") or "unknown")
        counts[lbl] = counts.get(lbl, 0) + 1
    return counts


def vip_prize_status_reference() -> dict[str, Any]:
    """Metadata lưu kèm file JSON / dùng khi implement claim."""
    return {
        "prize_status_legend": VIP_PRIZE_STATUS_LEGEND,
        "prize_status_notes": {str(k): v for k, v in VIP_PRIZE_STATUS_NOTES.items()},
        "claim_when_status": VIP_PRIZE_STATUS_CLAIMABLE,
        "updated": "2026-05-19",
        "verified": {"1": "co (mongtuongtu vipList)", "2": "chua", "3": "suy_doan"},
    }


def extract_vip_snapshot(data: dict[str, Any] | None) -> dict[str, Any]:
    """Từ vipList.data — cấp VIP hiện tại + % tiến độ lên cấp sau."""
    if not isinstance(data, dict):
        return {}
    vd = data.get("vip_data") or data.get("vipData")
    if not isinstance(vd, dict):
        return {}
    try:
        current = int(vd.get("current_level") or 0)
    except (TypeError, ValueError):
        current = 0
    try:
        progress = int(vd.get("progress") or 0)
    except (TypeError, ValueError):
        progress = 0
    progress = max(0, min(100, progress))
    level_label = f"VIP{current}" if current else ""
    levels = data.get("level_list") or data.get("levelList") or []
    if isinstance(levels, list) and current:
        for lv in levels:
            if isinstance(lv, dict) and int(lv.get("level") or 0) == current:
                level_label = str(lv.get("level_formatted") or level_label)
                break
    return {
        "current_level": current,
        "vip_level": level_label,
        "vip_progress": progress,
        "next_level": vd.get("next_level"),
        "completed_value": vd.get("completed_value"),
        "target_value": vd.get("target_value"),
        "reward_count": vd.get("reward_count"),
    }


def _persist_vip_db(
    username: str,
    account_id: str,
    data: dict[str, Any] | None,
    *,
    phase: str,
    quiet: bool,
) -> dict[str, Any]:
    from xoso66_vip_db import format_db_save_line, persist_vip_state

    snap = persist_vip_state(username, account_id, data, phase=phase)
    if snap.get("current_level") and not quiet:
        print(f"  [{username}] {format_db_save_line(snap)}", flush=True)
    return snap


def _find_level_row(data: dict[str, Any], current_level: int) -> dict[str, Any] | None:
    levels = data.get("level_list") or data.get("levelList") or []
    if not isinstance(levels, list):
        return None
    for lv in levels:
        if isinstance(lv, dict) and int(lv.get("level") or 0) == current_level:
            return lv
    return None


def _parse_prize_row(p: dict[str, Any]) -> dict[str, Any]:
    st = int(p.get("status") or 0)
    rt = str(p.get("reward_type") or "")
    return {
        "reward_type": rt,
        "title": p.get("title"),
        "prize": int(p.get("prize") or 0),
        "prize_fmt": _fmt_vnd(p.get("prize")),
        "status": st,
        "status_label": prize_status_label(st),
        "trang_thai": prize_status_vi(st),
        "nhan_chua": prize_nhan_chua_label(st),
    }


def extract_current_level_prizes(data: dict[str, Any] | None) -> dict[str, Any]:
    """Thưởng upgrade / weekly / monthly của cấp VIP đang có."""
    snap = extract_vip_snapshot(data)
    current = int(snap.get("current_level") or 0)
    out: dict[str, Any] = {
        "vip_level": snap.get("vip_level"),
        "vip_progress": snap.get("vip_progress"),
        "current_level": current,
        "next_level": snap.get("next_level"),
        "rewards": {},
    }
    if not current or not isinstance(data, dict):
        return out
    lv_row = _find_level_row(data, current)
    if not lv_row:
        return out
    by_type: dict[str, dict[str, Any]] = {}
    for p in lv_row.get("prize_list") or []:
        if not isinstance(p, dict):
            continue
        rt = str(p.get("reward_type") or "").strip().lower()
        if rt:
            by_type[rt] = _parse_prize_row(p)
    for key in ("upgrade", "weekly", "monthly"):
        if key in by_type:
            row = dict(by_type[key])
            row["loai"] = VIP_REWARD_TYPE_VI.get(key, key)
            out["rewards"][key] = row
    return out


def build_vip_report(result: dict[str, Any]) -> dict[str, Any]:
    """Báo cáo gọn cho in / JSON."""
    u = str(result.get("username") or "")
    if not result.get("ok"):
        return {
            "ok": False,
            "username": u,
            "error": result.get("error"),
        }
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    snap = result.get("vip_snapshot") or extract_vip_snapshot(data)
    prizes = extract_current_level_prizes(data)
    claims = result.get("claims") or []
    return {
        "ok": True,
        "username": u,
        "vip_level": snap.get("vip_level"),
        "vip_progress": snap.get("vip_progress"),
        "next_level": snap.get("next_level"),
        "rewards": prizes.get("rewards") or {},
        "claims": claims,
        "claims_ok": sum(1 for c in claims if c.get("ok")),
    }


def _print_vip_report(report: dict[str, Any]) -> None:
    u = str(report.get("username") or "?")
    print(f"\n{'=' * 56}", flush=True)
    print(f"  {u}", flush=True)
    print(f"{'=' * 56}", flush=True)
    if not report.get("ok"):
        print(f"  LỖI: {report.get('error')}", flush=True)
        return
    lv = str(report.get("vip_level") or "?")
    pct = int(report.get("vip_progress") or 0)
    nxt = report.get("next_level")
    print(f"  Đang VIP: {lv}  ({pct}% tiến độ lên VIP{nxt})", flush=True)
    rewards: dict[str, Any] = report.get("rewards") or {}
    if not rewards:
        print("  (không có prize_list cho cấp hiện tại)", flush=True)
        return
    w_loai = max(14, max((len(str(r.get("loai") or "")) for r in rewards.values()), default=8))
    hdr = (
        f"  {'Loại'.ljust(w_loai)}  "
        f"{'Số tiền'.rjust(14)}  "
        f"{'Nhận chưa?'.ljust(16)}  "
        f"Trạng thái"
    )
    print(hdr, flush=True)
    print(f"  {'-' * (len(hdr) - 2)}", flush=True)
    for key in ("upgrade", "weekly", "monthly"):
        r = rewards.get(key)
        if not r:
            continue
        print(
            f"  {str(r.get('loai') or key).ljust(w_loai)}  "
            f"{str(r.get('prize_fmt') or '—').rjust(14)}  "
            f"{str(r.get('nhan_chua') or '—').ljust(16)}  "
            f"{r.get('trang_thai')}",
            flush=True,
        )
    claims = report.get("claims") or []
    if claims:
        print("  Kết quả nhận thưởng:", flush=True)
        for c in claims:
            tag = "OK" if c.get("ok") else "FAIL"
            print(
                f"    {tag} {c.get('loai') or c.get('reward_type')}: "
                f"{c.get('msg')}",
                flush=True,
            )
    print(flush=True)


def _print_vip_reports_summary(reports: list[dict[str, Any]]) -> None:
    for rep in reports:
        _print_vip_report(rep)


def format_vip_db_line(snapshot: dict[str, Any]) -> str:
    lv = str(snapshot.get("vip_level") or "?")
    pct = int(snapshot.get("vip_progress") or 0)
    return f"DB vip_level={lv} progress={pct}%"


def summarize_vip_data(data: dict[str, Any] | None) -> dict[str, Any]:
    """Tóm tắt vip_data + phần thưởng theo status."""
    if not isinstance(data, dict):
        return {"empty": True}
    out: dict[str, Any] = {}
    snap = extract_vip_snapshot(data)
    if snap:
        out["vip_snapshot"] = snap
    vd = data.get("vip_data") or data.get("vipData")
    if isinstance(vd, dict):
        out["vip_data"] = vd
    out["prize_status_counts"] = _prize_status_counts(data)
    claimable = _collect_claimable_prizes(data)
    out["claimable_count"] = len(claimable)
    if claimable:
        out["claimable"] = claimable
    waiting = _collect_prizes_by_status(data, VIP_PRIZE_STATUS_WAITING_SCHEDULE)
    if waiting:
        out["waiting_schedule_count"] = len(waiting)
        out["waiting_schedule"] = waiting
    levels = data.get("level_list") or data.get("levelList")
    if isinstance(levels, list):
        out["level_list_count"] = len(levels)
    if not out:
        out["top_keys"] = sorted(data.keys())[:40]
    return out


def _print_vip_block_verbose(username: str, rep: dict[str, Any], *, saved_path: str | None) -> None:
    print(f"\n--- VIP [{username}] (verbose) ---", flush=True)
    if not rep.get("ok"):
        print(
            f"  Lỗi: code={rep.get('code')} msg={rep.get('msg')} "
            f"http={rep.get('http_status')}",
            flush=True,
        )
        if rep.get("raw_text"):
            print(f"  raw: {str(rep.get('raw_text'))[:300]}", flush=True)
        return
    summary = rep.get("summary") or {}
    print(f"  API OK — tóm tắt: {json.dumps(summary, ensure_ascii=False)}", flush=True)
    if saved_path:
        print(f"  Đã lưu: {saved_path}", flush=True)


def vip_after_ws_connect(
    account_id: str,
    username: str = "",
    *,
    do_claim: bool = True,
    force_login: bool = False,
) -> dict[str, Any]:
    """
    Gọi sau khi WS mini-game đã kết nối: GET vipList + nhận thưởng (giống CMS).
    `username` có thể rỗng — lấy từ DB theo account_id.
    """
    init_db()
    aid = str(account_id or "").strip()
    if not aid:
        return {"ok": False, "error": "missing account_id"}
    u = str(username or "").strip()
    if not u:
        row = get_account(aid)
        if row:
            u = str(row.get("username") or "").strip()
    if not u:
        return {"ok": False, "account_id": aid, "error": "missing username in DB"}
    return check_username(
        u,
        force_login=force_login,
        save_dir=None,
        verbose=False,
        quiet=True,
        do_claim=do_claim,
    )


def check_username(
    username: str,
    *,
    force_login: bool = False,
    save_dir: Path | None = None,
    verbose: bool = False,
    quiet: bool = False,
    do_claim: bool = True,
    reward_types: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    u = str(username or "").strip()
    row = get_account_by_username(u)
    if not row:
        by_id = get_account(u)
        if by_id:
            row = by_id
            u = str(row.get("username") or u)
        else:
            return {
                "ok": False,
                "username": u,
                "error": f"Không tìm thấy acc username/id={u!r} trong DB",
            }
    aid = str(row["id"])
    row_proxy = str(row.get("proxy") or "").strip()
    def _log(msg: str) -> None:
        if not quiet:
            print(msg, flush=True)

    try:
        _log(f"  [{u}] session/login ...")
        session = ensure_session(aid, force_login=force_login)
    except Exception as e:
        return {"ok": False, "username": u, "account_id": aid, "error": str(e)}

    from xoso66_proxy import proxy_source_label

    if verbose:
        _log(f"  [{u}] {proxy_source_label(session, account_proxy=row_proxy)}")
    _log(f"  [{u}] GET {VIP_LIST_PATH} ...")
    rep = fetch_vip_list(session)
    persist_session(aid, session)

    data = rep.get("data") if isinstance(rep.get("data"), dict) else {}
    summary = summarize_vip_data(data)
    vip_snap = extract_vip_snapshot(data)
    vip_db_snap: dict[str, Any] = {}
    if rep.get("ok"):
        vip_db_snap = _persist_vip_db(u, aid, data, phase="list", quiet=quiet)

    claims: list[dict[str, Any]] = []
    claimable = (
        collect_claimable_current_level(data, reward_types=reward_types)
        if rep.get("ok")
        else []
    )
    if claimable and do_claim:
        claims = execute_vip_claims(
            session,
            aid,
            u,
            claimable,
            account_proxy=row_proxy,
            quiet=quiet,
        )
        _log(f"  [{u}] vipList (sau nhận) ...")
        rep2 = fetch_vip_list(session)
        persist_session(aid, session)
        if rep2.get("ok"):
            data = rep2.get("data") if isinstance(rep2.get("data"), dict) else {}
            summary = summarize_vip_data(data)
            vip_snap = extract_vip_snapshot(data)
            vip_db_snap = _persist_vip_db(u, aid, data, phase="after_claim", quiet=quiet)
    elif claimable and not do_claim:
        _log(f"  [{u}] {len(claimable)} thưởng status=1 (--check-only, bỏ qua nhận)")

    saved_path: str | None = None
    if save_dir is not None:
        payload = {
            "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
            "username": u,
            "account_id": aid,
            "vip_level_db_before": str(row.get("vip_level") or ""),
            "vip_progress_db_before": int(row.get("vip_progress") or 0),
            "vip_db": vip_db_snap,
            **vip_prize_status_reference(),
            "api": {
                "ok": rep.get("ok"),
                "http_status": rep.get("http_status"),
                "code": rep.get("code"),
                "msg": rep.get("msg"),
            },
            "summary": summary,
            "data": data,
            "raw": rep.get("raw"),
        }
        saved_path = _save_payload(_snapshot_path(save_dir, u), payload)

    result: dict[str, Any] = {
        "ok": bool(rep.get("ok")),
        "username": u,
        "account_id": aid,
        "http_status": rep.get("http_status"),
        "api_code": rep.get("code"),
        "msg": rep.get("msg"),
        "summary": summary,
        "vip_snapshot": vip_snap,
        "vip_db": vip_db_snap,
        "claimable": claimable,
        "claims": claims,
        "claims_ok": sum(1 for c in claims if c.get("ok")),
        "data": data,
        "raw": rep.get("raw"),
        "saved_path": saved_path,
    }
    if not rep.get("ok"):
        result["error"] = str(rep.get("msg") or rep.get("raw_text") or "vipList thất bại")
    result["report"] = build_vip_report(result)
    if verbose:
        _print_vip_block_verbose(u, {**result, "ok": rep.get("ok")}, saved_path=saved_path)
    return result


def refresh_vip_batch(
    *,
    account_ids: list[str] | None = None,
    status_filter: str | None = "Đang Chơi",
    check_only: bool = False,
    force_login: bool = False,
    parallel: int = 8,
    save_snapshots: bool = False,
) -> dict[str, Any]:
    """CMS / API: vipList + nhận thưởng → account_vip."""
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

    save_dir = DEFAULT_SAVE_DIR if save_snapshots else None
    t0 = time.time()
    results: list[dict[str, Any]] = []
    workers = max(1, min(32, int(parallel or 8)))

    def _one(username: str) -> dict[str, Any]:
        return check_username(
            username,
            force_login=force_login,
            save_dir=save_dir,
            verbose=False,
            quiet=True,
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
                        {"ok": False, "username": futs[fut], "error": str(e)}
                    )

    ok_n = sum(1 for r in results if r.get("ok"))
    fail_n = len(results) - ok_n
    claimed_n = sum(int(r.get("claims_ok") or 0) for r in results)
    return {
        "ok": fail_n == 0,
        "total": len(results),
        "ok_count": ok_n,
        "fail_count": fail_n,
        "claims_ok": claimed_n,
        "check_only": bool(check_only),
        "elapsed_sec": round(time.time() - t0, 1),
        "results": results,
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


def _prompt_usernames_interactive() -> list[str]:
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


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="VIP: vipList + tự nhận activityreward (mặc định). --check-only = chỉ xem."
    )
    ap.add_argument("usernames", nargs="*", help="username hoặc account id")
    ap.add_argument("-f", "--file", help="file mỗi dòng 1 username")
    ap.add_argument("--all", dest="all_accounts", action="store_true")
    ap.add_argument("--status", help='lọc status CMS, vd. "Đang Chơi"')
    ap.add_argument("--json", action="store_true", help="in JSON kết quả ra stdout")
    ap.add_argument("--login", action="store_true", help="ép login lại")
    ap.add_argument(
        "--save-dir",
        default=str(DEFAULT_SAVE_DIR),
        help=f"thư mục lưu response (mặc định: {DEFAULT_SAVE_DIR.name})",
    )
    ap.add_argument(
        "--no-save",
        action="store_true",
        help="không ghi file JSON (chỉ in console)",
    )
    ap.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="in thêm proxy, JSON tóm tắt, đường dẫn file lưu",
    )
    ap.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="không in log login/API (chỉ bảng kết quả)",
    )
    ap.add_argument(
        "--check-only",
        action="store_true",
        help="chỉ xem vipList, không gọi activityreward",
    )
    ap.add_argument(
        "--claim-type",
        action="append",
        choices=("upgrade", "weekly", "monthly"),
        help="chỉ nhận loại thưởng (có thể lặp: --claim-type upgrade --claim-type weekly)",
    )
    args = ap.parse_args(argv)
    do_claim = not args.check_only
    claim_types = tuple(args.claim_type) if args.claim_type else None

    init_db()
    crypto_err = _require_crypto()
    if crypto_err:
        print(crypto_err, flush=True)
        return 1

    names = _collect_usernames(args)
    if not names:
        if sys.stdin.isatty():
            names = _prompt_usernames_interactive()
        if not names:
            return 0

    save_dir: Path | None = None
    if not args.no_save:
        save_dir = Path(args.save_dir)
        if not save_dir.is_absolute():
            save_dir = DIR / save_dir

    if not args.quiet:
        print(f"\nVIP check {len(names)} tài khoản → {VIP_LIST_PATH}\n", flush=True)
    results: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    t0 = time.time()
    for idx, name in enumerate(names, 1):
        if not args.quiet:
            print(f"[{idx}/{len(names)}] {name}", flush=True)
        row = get_account_by_username(name)
        uname = str((row or {}).get("username") or name)
        if not row:
            by_id = get_account(name)
            if by_id:
                uname = str(by_id.get("username") or name)
            else:
                results.append(
                    {
                        "ok": False,
                        "username": name,
                        "error": "không có trong DB",
                    }
                )
                continue
        row_result = check_username(
            uname,
            force_login=args.login,
            save_dir=save_dir,
            verbose=args.verbose,
            quiet=args.quiet,
            do_claim=do_claim,
            reward_types=claim_types,
        )
        results.append(row_result)
        reports.append(row_result.get("report") or build_vip_report(row_result))

    ok_n = sum(1 for r in results if r.get("ok"))

    if args.json:
        out = [
            {
                **(r.get("report") or {}),
                "saved_path": r.get("saved_path"),
                "account_id": r.get("account_id"),
            }
            if r.get("ok")
            else r
            for r in results
        ]
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        _print_vip_reports_summary(reports)
        claimed_n = sum(int(r.get("claims_ok") or 0) for r in results)
        print(
            f"[VIP] Xong — OK {ok_n}/{len(results)} ({round(time.time() - t0, 1)}s)"
            + (f", đã nhận {claimed_n} thưởng" if claimed_n else ""),
            flush=True,
        )
        if save_dir and args.verbose:
            print(f"[VIP] File JSON: {save_dir}", flush=True)

    return 0 if ok_n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
