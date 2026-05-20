# -*- coding: utf-8 -*-
"""
Tự động nhận thưởng nhiệm vụ sau khi acc «Đủ ngày».

Luồng (mỗi acc / ngày VN):
  1. Khi WS ép status → «Đủ ngày» (ngắt WS / đủ cap): hẹn check sau initial_delay_sec.
     Không quét lại toàn bộ acc Đủ ngày lúc khởi động main.
  2. mission/list + lưu DB; nếu có level status=1 (MINI 17 + điểm danh 22/161):
       balance ≥ min_withdraw_vnd → gọi API rút, poll lịch sử đến Hoàn tất, rồi mới POST reward.
       balance < min → chỉ nhận thưởng (không rút).
  3. Chưa claim được: nếu done_bet_money (161) < tổng cược ngày → poll mỗi poll_interval_sec,
     tối đa poll_max_attempts; nhận được thì dừng.
  4. Hết poll: sync done_bet_money local = tổng cược ngày → thử rút + nhận lại.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from xoso66_accounts_db import (
    daily_bet_today_vnd,
    db_conn,
    get_account,
    init_db,
    username_for_log,
)
from xoso66_config_util import load_config
from xoso66_shutdown import stopping
from xoso66_time_util import today_vn_str

_QUEUE_DDL = """
CREATE TABLE IF NOT EXISTS mission_auto_claim_queue (
    account_id TEXT PRIMARY KEY,
    vn_day TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'scheduled',
    scheduled_at REAL NOT NULL,
    poll_count INTEGER NOT NULL DEFAULT 0,
    reward_retry_count INTEGER NOT NULL DEFAULT 0,
    pending_claims_json TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
)
"""

_QUEUE_LOCK = threading.Lock()
_MIN_WITHDRAW_VND = 300_000


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _migrate_queue_columns(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(mission_auto_claim_queue)")}
    if "reward_retry_count" not in cols:
        conn.execute(
            "ALTER TABLE mission_auto_claim_queue "
            "ADD COLUMN reward_retry_count INTEGER NOT NULL DEFAULT 0"
        )
    if "pending_claims_json" not in cols:
        conn.execute(
            "ALTER TABLE mission_auto_claim_queue "
            "ADD COLUMN pending_claims_json TEXT NOT NULL DEFAULT ''"
        )


def init_mission_claim_queue(conn: sqlite3.Connection | None = None) -> None:
    if conn is None:
        init_db()
        with db_conn() as c:
            c.execute(_QUEUE_DDL)
            _migrate_queue_columns(c)
        return
    conn.execute(_QUEUE_DDL)
    _migrate_queue_columns(conn)


def _cfg() -> dict[str, Any]:
    raw = load_config().get("auto_mission_reward")
    return raw if isinstance(raw, dict) else {}


def auto_mission_reward_enabled() -> bool:
    return bool(_cfg().get("enabled", False))


def _initial_delay_sec() -> float:
    return float(_cfg().get("initial_delay_sec", 300))


def _poll_interval_sec() -> float:
    return float(_cfg().get("poll_interval_sec", 60))


def _poll_max_attempts() -> int:
    return int(_cfg().get("poll_max_attempts", 15))


def _min_withdraw_vnd() -> int:
    return int(_cfg().get("min_withdraw_vnd", _MIN_WITHDRAW_VND))


def _sync_balance_to_db(
    account_id: str,
    session: dict,
    *,
    label: str = "",
) -> float | None:
    from xoso66_session import refresh_account_balance_to_db

    if label:
        pass  # ngữ cảnh (trước/sau rút, v.v.) — dòng log chính từ get_user_balance

    aid = str(account_id).strip()
    rep = refresh_account_balance_to_db(aid, session, refresh=True)
    if rep.get("ok") and rep.get("balance") is not None:
        return float(rep["balance"])
    return None


def _start_withdraw_confirm_watch(
    account_id: str,
    *,
    amount_vnd: int,
    since_ms: int,
    serial_no: str | None = None,
) -> None:
    from xoso66_withdraw_tracking import start_withdraw_confirm_watch

    start_withdraw_confirm_watch(
        account_id,
        amount_vnd=amount_vnd,
        since_ms=since_ms,
        serial_no=serial_no,
        log_prefix="[AUTO-MISSION]",
        notify_on_fail=True,
    )


def _reward_retry_delay_sec() -> float:
    return float(_cfg().get("reward_retry_delay_sec", 300))


def _reward_retry_max() -> int:
    return int(_cfg().get("reward_retry_max", 3))


def _pending_claim_keys(raw: str) -> set[tuple[int, int]]:
    try:
        items = json.loads(raw or "[]")
    except Exception:
        return set()
    if not isinstance(items, list):
        return set()
    out: set[tuple[int, int]] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            out.add((int(it["mission_id"]), int(it["level_id"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _pending_claims_to_json(keys: set[tuple[int, int]]) -> str:
    return json.dumps(
        [{"mission_id": m, "level_id": l} for m, l in sorted(keys)],
        separators=(",", ":"),
    )


def _rate_limit_failures(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from xoso66_daily_mission_check import is_mission_reward_rate_limit

    out: list[dict[str, Any]] = []
    for c in claims:
        if c.get("ok"):
            continue
        if is_mission_reward_rate_limit(str(c.get("msg") or "")):
            out.append(c)
    return out


def floor_withdraw_amount_vnd(balance: float) -> int:
    """Bỏ phần lẻ (vd. ,22đ) — rút số nguyên VND."""
    try:
        b = float(balance)
    except (TypeError, ValueError):
        return 0
    if b < 0:
        return 0
    return int(b)


def schedule_mission_claim(account_id: str, *, reason: str = "") -> bool:
    """
    Hẹn nhận thưởng cho acc (sau initial_delay_sec). Trả False nếu tắt auto hoặc đã có hàng đợi hôm nay.
    """
    if not auto_mission_reward_enabled():
        return False
    aid = str(account_id or "").strip()
    if not aid:
        return False
    row = get_account(aid)
    if not row:
        return False
    vn_day = today_vn_str()
    at = time.time() + _initial_delay_sec()
    init_mission_claim_queue()
    tag = f" ({reason})" if reason else ""
    with _QUEUE_LOCK:
        with db_conn() as conn:
            cur = conn.execute(
                "SELECT phase, vn_day FROM mission_auto_claim_queue WHERE account_id = ?",
                (aid,),
            ).fetchone()
            if cur:
                phase = str(cur["phase"] or "")
                if str(cur["vn_day"] or "") == vn_day and phase in ("scheduled", "polling"):
                    return False
                if str(cur["vn_day"] or "") == vn_day and phase == "done":
                    return False
            conn.execute(
                """
                INSERT INTO mission_auto_claim_queue (
                    account_id, vn_day, phase, scheduled_at, poll_count, last_error, updated_at
                ) VALUES (?, ?, 'scheduled', ?, 0, '', ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    vn_day=excluded.vn_day,
                    phase='scheduled',
                    scheduled_at=excluded.scheduled_at,
                    poll_count=0,
                    reward_retry_count=0,
                    pending_claims_json='',
                    last_error='',
                    updated_at=excluded.updated_at
                """,
                (aid, vn_day, at, _now_iso()),
            )
    u = username_for_log(aid, row)
    print(
        f"[AUTO-MISSION] Hẹn {u}: check nhận thưởng sau {_initial_delay_sec():.0f}s{tag}",
        flush=True,
    )
    return True


def schedule_mission_claim_many(account_ids: list[str], *, reason: str = "") -> int:
    n = 0
    for aid in account_ids:
        if schedule_mission_claim(aid, reason=reason):
            n += 1
    return n


def _queue_update(
    account_id: str,
    *,
    phase: str | None = None,
    scheduled_at: float | None = None,
    poll_count: int | None = None,
    reward_retry_count: int | None = None,
    pending_claims_json: str | None = None,
    last_error: str | None = None,
    vn_day: str | None = None,
) -> None:
    aid = str(account_id).strip()
    sets: list[str] = ["updated_at = ?"]
    vals: list[Any] = [_now_iso()]
    if phase is not None:
        sets.append("phase = ?")
        vals.append(phase)
    if scheduled_at is not None:
        sets.append("scheduled_at = ?")
        vals.append(float(scheduled_at))
    if poll_count is not None:
        sets.append("poll_count = ?")
        vals.append(int(poll_count))
    if reward_retry_count is not None:
        sets.append("reward_retry_count = ?")
        vals.append(int(reward_retry_count))
    if pending_claims_json is not None:
        sets.append("pending_claims_json = ?")
        vals.append(str(pending_claims_json)[:4000])
    if last_error is not None:
        sets.append("last_error = ?")
        vals.append(str(last_error)[:500])
    if vn_day is not None:
        sets.append("vn_day = ?")
        vals.append(vn_day)
    vals.append(aid)
    with _QUEUE_LOCK:
        with db_conn() as conn:
            conn.execute(
                f"UPDATE mission_auto_claim_queue SET {', '.join(sets)} WHERE account_id = ?",
                vals,
            )


def _due_queue_rows() -> list[dict[str, Any]]:
    init_mission_claim_queue()
    now = time.time()
    vn_day = today_vn_str()
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM mission_auto_claim_queue
            WHERE vn_day = ? AND phase IN ('scheduled', 'polling', 'reward_retry')
              AND scheduled_at <= ?
            ORDER BY scheduled_at ASC
            """,
            (vn_day, now),
        ).fetchall()
    return [dict(r) for r in rows]


def _account_balance_vnd(account_id: str, session: dict) -> float:
    bal = _sync_balance_to_db(account_id, session, label="trước rút")
    if bal is not None:
        return bal
    row = get_account(account_id) or {}
    return float(row.get("balance") or 0)


def _maybe_withdraw_before_claim(
    account_id: str,
    session: dict,
    row: dict[str, Any],
    *,
    min_vnd: int,
) -> dict[str, Any]:
    """Rút toàn bộ balance nếu ≥ min_vnd; trả summary."""
    from xoso66_withdraw import resolve_fund_password, withdraw_for_account

    u = username_for_log(account_id, row)
    bal = _account_balance_vnd(account_id, session)
    amt = floor_withdraw_amount_vnd(bal)
    out: dict[str, Any] = {
        "balance": bal,
        "withdraw_amount": 0,
        "withdraw_ok": None,
        "skipped": True,
        "reason": "",
    }
    if amt < min_vnd:
        out["reason"] = f"balance {amt:,} < {min_vnd:,}"
        print(f"[AUTO-MISSION] {u}: bỏ rút — {out['reason']}", flush=True)
        return out

    fund = str(row.get("fund_password") or session.get("fund_password") or "").strip()
    try:
        fund_pwd = resolve_fund_password(session, fund)
    except Exception as e:
        out["reason"] = f"thiếu MK rút: {e}"
        print(f"[AUTO-MISSION] {u}: bỏ rút — {out['reason']}", flush=True)
        return out

    out["skipped"] = False
    out["withdraw_amount"] = amt
    print(f"[AUTO-MISSION] {u}: rút {amt:,}đ (trước nhận thưởng) ...", flush=True)
    since_ms = int(time.time() * 1000)
    try:
        wr = withdraw_for_account(account_id, amt, fund_pwd, verify=True)
        out["withdraw_ok"] = bool(wr.get("ok"))
        out["withdraw_msg"] = wr.get("msg") or wr.get("reason") or ""
        out["withdraw_raw"] = wr
        tag = "OK" if wr.get("ok") else "FAIL"
        print(f"[AUTO-MISSION] {u}: rút {tag} — {out.get('withdraw_msg')}", flush=True)
        if wr.get("ok"):
            _sync_balance_to_db(account_id, session, label="sau API rút")
            from xoso66_withdraw_tracking import (
                extract_withdraw_serial,
                poll_withdraw_until_confirmed,
                withdraw_confirm_poll_interval_sec,
                withdraw_confirm_poll_max,
            )

            serial = extract_withdraw_serial(wr)
            out["withdraw_serial"] = serial
            interval = withdraw_confirm_poll_interval_sec()
            max_attempts = withdraw_confirm_poll_max()
            print(
                f"[AUTO-MISSION] {u}: chờ rút Hoàn tất trên site — "
                f"poll {interval:.0f}s × {max_attempts}"
                + (f" (serial {serial})" if serial else ""),
                flush=True,
            )
            poll_rep = poll_withdraw_until_confirmed(
                session,
                account_id=account_id,
                amount_vnd=amt,
                since_ms=since_ms,
                poll_interval_sec=interval,
                max_attempts=max_attempts,
                serial_no=serial,
                log_prefix="[AUTO-MISSION]",
            )
            out["withdraw_poll"] = poll_rep
            confirmed = bool(poll_rep.get("confirmed") and poll_rep.get("success"))
            out["withdraw_confirmed"] = confirmed
            if confirmed:
                att = int(poll_rep.get("attempt") or 0)
                print(
                    f"[AUTO-MISSION] {u}: nhận thưởng nhiệm vụ "
                    f"(sau khi rút OK lần check {att})",
                    flush=True,
                )
                _sync_balance_to_db(account_id, session, label="sau rút Hoàn tất")
                out["withdraw_ok"] = True
            else:
                err = str(
                    poll_rep.get("error")
                    or (poll_rep.get("last_check") or {}).get("hint")
                    or "chưa thấy Hoàn tất"
                )
                out["withdraw_ok"] = False
                out["withdraw_msg"] = err
                print(
                    f"[AUTO-MISSION] {u}: chưa xác nhận Hoàn tất — bỏ nhận thưởng ({err})",
                    flush=True,
                )
                _start_withdraw_confirm_watch(
                    account_id,
                    amount_vnd=amt,
                    since_ms=since_ms,
                    serial_no=serial,
                )
    except Exception as e:
        out["withdraw_ok"] = False
        out["withdraw_msg"] = str(e)
        print(f"[AUTO-MISSION] {u}: rút lỗi — {e}", flush=True)
    return out


def _can_claim_after_withdraw(withdraw_info: dict[str, Any] | None) -> bool:
    """
    True nếu được phép nhận thưởng:
    - Không bước rút (retry sau khi đã rút + nhận).
    - Bỏ rút vì balance < min_withdraw_vnd.
    - Đã rút và lịch sử site xác nhận Hoàn tất (withdraw_confirmed).
    """
    if withdraw_info is None:
        return True
    if withdraw_info.get("skipped"):
        reason = str(withdraw_info.get("reason") or "")
        return "balance" in reason and "<" in reason
    return bool(withdraw_info.get("withdraw_ok"))


def _daily_done_bet_from_levels(levels: list[dict[str, Any]]) -> int:
    from xoso66_daily_mission_check import DAILY_LEVEL_ID

    for lv in levels:
        if int(lv.get("level_id") or 0) == DAILY_LEVEL_ID:
            return int(lv.get("done_bet_money") or 0)
    return 0


def _run_claim_flow(
    account_id: str,
    *,
    do_claim: bool = True,
    withdraw_before: bool = True,
    only_level_keys: set[tuple[int, int]] | None = None,
) -> dict[str, Any]:
    """mission/list → (rút) → reward → list + DB."""
    from xoso66_daily_mission_check import (
        REWARD_CLAIM_STATUS,
        collect_tracked_levels,
        execute_mission_claims,
        fetch_mission_list,
    )
    from xoso66_mission_db import format_db_save_line, persist_mission_state
    from xoso66_session import ensure_session, persist_session

    aid = str(account_id).strip()
    row = get_account(aid)
    if not row:
        return {"ok": False, "error": "account not found"}
    u = str(row.get("username") or aid)
    row_proxy = str(row.get("proxy") or "").strip()

    try:
        session = ensure_session(aid, force_login=False)
    except Exception as e:
        return {"ok": False, "username": u, "account_id": aid, "error": str(e)}

    rep = fetch_mission_list(session)
    persist_session(aid, session)
    if not rep.get("ok"):
        return {
            "ok": False,
            "username": u,
            "account_id": aid,
            "error": str(rep.get("msg") or "mission/list thất bại"),
        }

    data = rep.get("data") or {}
    levels = collect_tracked_levels(data)
    mission_snap = persist_mission_state(u, aid, levels, phase="list")

    claimable = [x for x in levels if x.get("status") == REWARD_CLAIM_STATUS]
    if only_level_keys:
        claimable = [
            x
            for x in claimable
            if (int(x["mission_id"]), int(x["level_id"])) in only_level_keys
        ]
    withdraw_info: dict[str, Any] | None = None
    claims: list[dict[str, Any]] = []
    claim_blocked_by_withdraw = False

    if claimable and do_claim:
        if withdraw_before:
            withdraw_info = _maybe_withdraw_before_claim(
                aid, session, row, min_vnd=_min_withdraw_vnd()
            )
        if _can_claim_after_withdraw(withdraw_info):
            claims = execute_mission_claims(
                session, aid, u, claimable, account_proxy=row_proxy
            )
            if sum(1 for c in claims if c.get("ok")) > 0:
                _sync_balance_to_db(aid, session, label="sau nhận thưởng")
            rep2 = fetch_mission_list(session)
            persist_session(aid, session)
            if rep2.get("ok"):
                data = rep2.get("data") or {}
                levels = collect_tracked_levels(data)
                mission_snap = persist_mission_state(
                    u, aid, levels, phase="after_claim"
                )
                print(
                    f"[AUTO-MISSION] {u}: {format_db_save_line(mission_snap)}",
                    flush=True,
                )
        else:
            claim_blocked_by_withdraw = True
            detail = (
                withdraw_info.get("withdraw_msg")
                or withdraw_info.get("reason")
                or "rút thất bại"
            )
            print(
                f"[AUTO-MISSION] {u}: bỏ nhận thưởng — chưa đủ điều kiện rút ({detail})",
                flush=True,
            )

    done_bet = _daily_done_bet_from_levels(levels)
    daily_total = int(daily_bet_today_vnd(row))

    return {
        "ok": True,
        "username": u,
        "account_id": aid,
        "claimable_count": len(claimable),
        "claims": claims,
        "claims_ok": sum(1 for c in claims if c.get("ok")),
        "withdraw": withdraw_info,
        "done_bet_money": done_bet,
        "daily_bet_total": daily_total,
        "mission_db": mission_snap,
        "had_claimable": bool(claimable),
        "only_retry_levels": bool(only_level_keys),
        "claim_blocked_by_withdraw": claim_blocked_by_withdraw,
    }


def _schedule_reward_retry(
    account_id: str,
    reward_retry_count: int,
    pending_keys: set[tuple[int, int]],
    *,
    reason: str,
) -> None:
    nxt = reward_retry_count + 1
    at = time.time() + _reward_retry_delay_sec()
    _queue_update(
        account_id,
        phase="reward_retry",
        scheduled_at=at,
        reward_retry_count=nxt,
        pending_claims_json=_pending_claims_to_json(pending_keys),
        last_error=reason,
    )
    u = username_for_log(account_id)
    levels = ", ".join(f"m{m}/lv{l}" for m, l in sorted(pending_keys))
    print(
        f"[AUTO-MISSION] {u}: hẹn nhận lại ({levels}) sau "
        f"{_reward_retry_delay_sec():.0f}s — lần {nxt}/{_reward_retry_max()} "
        f"(rate-limit)",
        flush=True,
    )


def _try_finish_after_claims(
    aid: str,
    u: str,
    result: dict[str, Any],
    *,
    poll_count: int,
    reward_retry_count: int,
) -> bool:
    """
    Xử lý rate-limit retry / đánh dấu done. Trả True nếu đã kết thúc hàng đợi acc.
    """
    claims = result.get("claims") or []
    claims_ok = int(result.get("claims_ok") or 0)
    had_claimable = bool(result.get("had_claimable"))
    done_bet = int(result.get("done_bet_money") or 0)
    daily_total = int(result.get("daily_bet_total") or 0)
    only_retry = bool(result.get("only_retry_levels"))

    rate_fails = _rate_limit_failures(claims)
    if rate_fails:
        pending_keys = {
            (int(c["mission_id"]), int(c["level_id"])) for c in rate_fails
        }
        if reward_retry_count < _reward_retry_max():
            _schedule_reward_retry(
                aid,
                reward_retry_count,
                pending_keys,
                reason=str(rate_fails[0].get("msg") or "rate-limit")[:200],
            )
            if claims_ok > 0:
                print(
                    f"[AUTO-MISSION] {u}: đã nhận {claims_ok} mức; "
                    f"chờ retry {len(pending_keys)} mức còn lại",
                    flush=True,
                )
            return True

        levels = ", ".join(f"m{m}/lv{l}" for m, l in sorted(pending_keys))
        print(
            f"[AUTO-MISSION] {u}: bỏ qua sau {_reward_retry_max()} lần retry — "
            f"vẫn lỗi rate-limit ({levels})",
            flush=True,
        )
        _queue_update(
            aid,
            phase="done",
            pending_claims_json="",
            last_error=f"bỏ qua rate-limit: {levels}",
        )
        if claims_ok > 0:
            print(
                f"[AUTO-MISSION] {u}: nhận thưởng xong ({claims_ok} mức, một phần bỏ qua)",
                flush=True,
            )
        return True

    if only_retry:
        _queue_update(aid, phase="done", pending_claims_json="", last_error="")
        print(f"[AUTO-MISSION] {u}: retry nhận thưởng xong ({claims_ok} mức)", flush=True)
        return True

    if had_claimable and claims_ok > 0:
        _queue_update(aid, phase="done", pending_claims_json="", last_error="")
        print(f"[AUTO-MISSION] {u}: nhận thưởng xong ({claims_ok} mức)", flush=True)
        return True

    if result.get("claim_blocked_by_withdraw"):
        w = result.get("withdraw") or {}
        detail = w.get("withdraw_msg") or w.get("reason") or "chưa rút OK"
        _reschedule_poll(aid, poll_count, f"chờ rút OK — {detail}")
        return True

    if had_claimable and claims_ok == 0:
        err = "có mức claimable nhưng reward thất bại"
        _queue_update(aid, phase="polling", last_error=err)
        _reschedule_poll(aid, poll_count, err)
        return True

    if done_bet < daily_total and daily_total > 0:
        if poll_count >= _poll_max_attempts():
            from xoso66_mission_db import force_daily_done_bet_to_account_total

            synced = force_daily_done_bet_to_account_total(aid)
            print(
                f"[AUTO-MISSION] {u}: hết {poll_count} lần poll — "
                f"sync done_bet_money={synced:,} (= tổng cược ngày) → thử nhận lại",
                flush=True,
            )
            retry = _run_claim_flow(aid, do_claim=True, withdraw_before=True)
            if retry.get("ok"):
                return _try_finish_after_claims(
                    aid,
                    u,
                    retry,
                    poll_count=poll_count,
                    reward_retry_count=reward_retry_count,
                )
            _queue_update(
                aid,
                phase="failed",
                last_error=str(retry.get("error") or "sync xong, vẫn chưa nhận được"),
            )
            return True

        _reschedule_poll(
            aid,
            poll_count,
            f"done_bet {done_bet:,} < cược ngày {daily_total:,}",
        )
        return True

    if not had_claimable:
        _queue_update(aid, phase="done", pending_claims_json="", last_error="")
        print(
            f"[AUTO-MISSION] {u}: không có mức status=1 — coi xong "
            f"(done_bet={done_bet:,}, cược ngày={daily_total:,})",
            flush=True,
        )
        return True

    _queue_update(aid, phase="done", pending_claims_json="", last_error="")
    return True


def _process_queue_row(qrow: dict[str, Any]) -> None:
    aid = str(qrow.get("account_id") or "").strip()
    if not aid:
        return
    poll_count = int(qrow.get("poll_count") or 0)
    reward_retry_count = int(qrow.get("reward_retry_count") or 0)
    phase = str(qrow.get("phase") or "")
    pending_keys = _pending_claim_keys(str(qrow.get("pending_claims_json") or ""))
    u = username_for_log(aid)

    is_reward_retry = phase == "reward_retry" and bool(pending_keys)
    if is_reward_retry:
        print(
            f"[AUTO-MISSION] {u}: retry nhận thưởng (lần {reward_retry_count}/"
            f"{_reward_retry_max()}), không rút lại",
            flush=True,
        )

    try:
        result = _run_claim_flow(
            aid,
            do_claim=True,
            withdraw_before=not is_reward_retry,
            only_level_keys=pending_keys if is_reward_retry else None,
        )
    except Exception as e:
        _queue_update(aid, phase="polling", last_error=str(e))
        print(f"[AUTO-MISSION] {u}: lỗi luồng — {e}", flush=True)
        return

    if not result.get("ok"):
        err = str(result.get("error") or "unknown")
        if is_reward_retry and reward_retry_count < _reward_retry_max():
            _schedule_reward_retry(
                aid,
                reward_retry_count,
                pending_keys,
                reason=err,
            )
            return
        _queue_update(aid, phase="polling", last_error=err)
        _reschedule_poll(aid, poll_count, err)
        return

    if _try_finish_after_claims(
        aid,
        u,
        result,
        poll_count=poll_count,
        reward_retry_count=reward_retry_count,
    ):
        return

def _reschedule_poll(account_id: str, poll_count: int, reason: str) -> None:
    nxt = poll_count + 1
    at = time.time() + _poll_interval_sec()
    _queue_update(
        account_id,
        phase="polling",
        scheduled_at=at,
        poll_count=nxt,
        last_error=reason,
    )
    u = username_for_log(account_id)
    print(
        f"[AUTO-MISSION] {u}: poll {nxt}/{_poll_max_attempts()} sau "
        f"{_poll_interval_sec():.0f}s — {reason}",
        flush=True,
    )


def worker_auto_mission_reward_loop(*, quiet: bool = False) -> None:
    if not auto_mission_reward_enabled():
        return
    init_mission_claim_queue()
    tick = float(_cfg().get("worker_tick_sec", 10))
    if not quiet:
        print(
            f"[AUTO-MISSION] Worker: delay đầu {_initial_delay_sec():.0f}s, "
            f"poll {_poll_interval_sec():.0f}s × {_poll_max_attempts()}, "
            f"rate-limit retry {_reward_retry_delay_sec():.0f}s × {_reward_retry_max()}, "
            f"rút ≥ {_min_withdraw_vnd():,}đ → chờ Hoàn tất site rồi mới nhận thưởng",
            flush=True,
        )
    while not stopping():
        try:
            for qrow in _due_queue_rows():
                if stopping():
                    break
                _process_queue_row(qrow)
        except Exception as e:
            if not stopping():
                print(f"[AUTO-MISSION] Lỗi vòng quét: {e}", flush=True)
        for _ in range(max(1, int(tick))):
            if stopping():
                break
            time.sleep(1)
    print("[AUTO-MISSION] Worker đã dừng.", flush=True)


def start_auto_mission_reward_thread(*, quiet: bool = False) -> threading.Thread | None:
    if not auto_mission_reward_enabled():
        return None
    t = threading.Thread(
        target=worker_auto_mission_reward_loop,
        kwargs={"quiet": quiet},
        daemon=False,
        name="xoso66-auto-mission",
    )
    t.start()
    return t
