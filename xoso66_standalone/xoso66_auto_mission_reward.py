# -*- coding: utf-8 -*-
"""
Tự động nhận thưởng nhiệm vụ sau khi acc «Đủ ngày».

Luồng (mỗi acc / ngày VN):
  1. Khi WS ép status → «Đủ ngày» (ngắt WS / đủ cap): hẹn check sau initial_delay_sec.
     Không quét lại toàn bộ acc Đủ ngày lúc khởi động main.
  2. mission/list + lưu DB; nếu có level status=1 (MINI 17 + điểm danh 22/161):
       balance ≥ min_withdraw_vnd → rút bội withdraw_step_vnd (vd. 300k/400k/500k), poll Hoàn tất, POST reward.
       balance < min → chỉ nhận thưởng (không rút).
  3. Chưa claim được: poll chỉ khi done_bet < 888888 VÀ done_bet < tổng cược ngày,
     tối đa poll_max_attempts; nhận được thì dừng. Trong lúc poll: không ghi đè
     accounts.daily_bet_total bằng 161 (giữ tổng cược thật trên DB).
  4. Hết poll (15/15): sync done_bet_money = tổng cược ngày (accounts + mission) → thử rút + nhận lại.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from xoso66_accounts_db import (
    daily_bet_today_vnd,
    get_account,
    username_for_log,
)
from xoso66_config_util import load_config
from xoso66_paths import cms_game_data_dir
from xoso66_shutdown import stopping
from xoso66_time_util import today_vn_str

_QUEUE_LOCK = threading.Lock()
_QUEUE_FILE = Path(cms_game_data_dir()) / "mission_auto_claim_queue.json"
_MIN_WITHDRAW_VND = 300_000
_WITHDRAW_STEP_VND = 100_000


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _normalize_queue_item(account_id: str, item: dict[str, Any]) -> dict[str, Any]:
    return {
        "account_id": str(account_id),
        "vn_day": str(item.get("vn_day") or today_vn_str()),
        "phase": str(item.get("phase") or "scheduled"),
        "scheduled_at": float(item.get("scheduled_at") or 0),
        "poll_count": int(item.get("poll_count") or 0),
        "reward_retry_count": int(item.get("reward_retry_count") or 0),
        "pending_claims_json": str(item.get("pending_claims_json") or "")[:4000],
        "last_error": str(item.get("last_error") or "")[:500],
        "updated_at": str(item.get("updated_at") or _now_iso()),
    }


def _load_queue_map() -> dict[str, dict[str, Any]]:
    init_mission_claim_queue()
    try:
        raw = json.loads(_QUEUE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for k, v in raw.items():
        if not isinstance(v, dict):
            continue
        aid = str(k or "").strip()
        if not aid:
            continue
        out[aid] = _normalize_queue_item(aid, v)
    return out


def _save_queue_map(items: dict[str, dict[str, Any]]) -> None:
    _QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {aid: _normalize_queue_item(aid, row) for aid, row in items.items()}
    _QUEUE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def init_mission_claim_queue(conn: object | None = None) -> None:
    _ = conn
    _QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _QUEUE_FILE.exists():
        return
    _QUEUE_FILE.write_text("{}", encoding="utf-8")


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


def _withdraw_step_vnd() -> int:
    return max(1, int(_cfg().get("withdraw_step_vnd", _WITHDRAW_STEP_VND)))


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


def _all_failures_ip_already_claimed(claims: list[dict[str, Any]]) -> bool:
    from xoso66_daily_mission_check import is_mission_reward_ip_already_claimed

    failed = [c for c in claims if not c.get("ok")]
    if not failed:
        return False
    return all(
        is_mission_reward_ip_already_claimed(str(c.get("msg") or "")) for c in failed
    )


def floor_withdraw_amount_vnd(
    balance: float,
    *,
    min_vnd: int | None = None,
    step_vnd: int | None = None,
) -> int:
    """
    Số tiền rút tự động: bội step_vnd (mặc định 100k), không vượt số dư.
    Trả 0 nếu balance < min_vnd hoặc không đủ một bội ≥ min (vd. 250k → 0).
    """
    min_v = int(min_vnd if min_vnd is not None else _min_withdraw_vnd())
    step = max(1, int(step_vnd if step_vnd is not None else _withdraw_step_vnd()))
    try:
        b = float(balance)
    except (TypeError, ValueError):
        return 0
    if b < min_v:
        return 0
    avail = int(b)
    if avail < 0:
        return 0
    amt = (avail // step) * step
    return amt if amt >= min_v else 0


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
    tag = f" ({reason})" if reason else ""
    with _QUEUE_LOCK:
        items = _load_queue_map()
        cur = items.get(aid) or {}
        phase = str(cur.get("phase") or "")
        if str(cur.get("vn_day") or "") == vn_day and phase in ("scheduled", "polling", "done"):
            return False
        items[aid] = _normalize_queue_item(
            aid,
            {
                "vn_day": vn_day,
                "phase": "scheduled",
                "scheduled_at": at,
                "poll_count": 0,
                "reward_retry_count": 0,
                "pending_claims_json": "",
                "last_error": "",
                "updated_at": _now_iso(),
            },
        )
        _save_queue_map(items)
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


def cancel_mission_claim_queue(
    account_id: str = "",
    *,
    username: str = "",
) -> bool:
    """Xóa hàng đợi auto-mission — dừng poll/retry (DB mission_auto_claim_queue)."""
    from xoso66_accounts_db import get_account_by_username

    aid = str(account_id or "").strip()
    if not aid and username:
        row = get_account_by_username(username)
        aid = str((row or {}).get("id") or "").strip()
    if not aid:
        return False
    with _QUEUE_LOCK:
        items = _load_queue_map()
        deleted = aid in items
        if deleted:
            items.pop(aid, None)
            _save_queue_map(items)
    if deleted:
        print(
            f"[AUTO-MISSION] Đã xóa hàng đợi: {username_for_log(aid)}",
            flush=True,
        )
    return deleted


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
    with _QUEUE_LOCK:
        items = _load_queue_map()
        cur = items.get(aid)
        if not cur:
            return
        nxt = dict(cur)
        nxt["updated_at"] = _now_iso()
        if phase is not None:
            nxt["phase"] = phase
        if scheduled_at is not None:
            nxt["scheduled_at"] = float(scheduled_at)
        if poll_count is not None:
            nxt["poll_count"] = int(poll_count)
        if reward_retry_count is not None:
            nxt["reward_retry_count"] = int(reward_retry_count)
        if pending_claims_json is not None:
            nxt["pending_claims_json"] = str(pending_claims_json)[:4000]
        if last_error is not None:
            nxt["last_error"] = str(last_error)[:500]
        if vn_day is not None:
            nxt["vn_day"] = vn_day
        items[aid] = _normalize_queue_item(aid, nxt)
        _save_queue_map(items)


def _due_queue_rows() -> list[dict[str, Any]]:
    now = time.time()
    vn_day = today_vn_str()
    with _QUEUE_LOCK:
        rows = list(_load_queue_map().values())
    out = [
        r
        for r in rows
        if str(r.get("vn_day") or "") == vn_day
        and str(r.get("phase") or "") in ("scheduled", "polling", "reward_retry")
        and float(r.get("scheduled_at") or 0) <= now
    ]
    out.sort(key=lambda x: float(x.get("scheduled_at") or 0))
    return out


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
    """Rút bội step (mặc định 100k) nếu balance ≥ min_vnd; trả summary."""
    from xoso66_withdraw import resolve_fund_password, withdraw_for_account

    u = username_for_log(account_id, row)
    bal = _account_balance_vnd(account_id, session)
    step = _withdraw_step_vnd()
    amt = floor_withdraw_amount_vnd(bal, min_vnd=min_vnd, step_vnd=step)
    out: dict[str, Any] = {
        "balance": bal,
        "withdraw_amount": 0,
        "withdraw_ok": None,
        "skipped": True,
        "reason": "",
    }
    if amt <= 0:
        out["reason"] = (
            f"balance {int(bal):,} < {min_vnd:,} "
            f"hoặc không đủ bội {step:,} (≥ {min_vnd:,})"
        )
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
        if not wr.get("ok"):
            from xoso66_account_errors import maybe_mark_account_loi

            maybe_mark_account_loi(
                account_id,
                str(out.get("withdraw_msg") or wr.get("msg") or ""),
                source="withdraw",
            )
        if wr.get("ok"):
            _sync_balance_to_db(account_id, session, label="sau API rút")
            from xoso66_withdraw_tracking import (
                extract_withdraw_serial,
                poll_withdraw_until_confirmed,
                disable_auto_bet_on_withdraw_timeout,
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
                stop_program_on_exhausted=disable_auto_bet_on_withdraw_timeout(),
            )
            out["withdraw_poll"] = poll_rep
            confirmed = bool(poll_rep.get("confirmed") and poll_rep.get("success"))
            out["withdraw_confirmed"] = confirmed
            if confirmed:
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
                from xoso66_config_util import load_config

                ab = load_config().get("auto_bet")
                if isinstance(ab, dict) and ab.get("enabled"):
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
    sync_accounts_daily_bet: bool | None = None,
) -> dict[str, Any]:
    """mission/list → (rút) → reward → list + DB."""
    from xoso66_daily_mission_check import (
        REWARD_CLAIM_STATUS,
        collect_tracked_levels,
        execute_mission_claims,
        fetch_mission_list,
    )
    from xoso66_mission_db import persist_mission_state
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
        err = str(rep.get("msg") or "mission/list thất bại")
        from xoso66_account_errors import maybe_mark_account_loi

        maybe_mark_account_loi(aid, err, source="mission/list")
        return {
            "ok": False,
            "username": u,
            "account_id": aid,
            "error": err,
        }

    data = rep.get("data") or {}
    levels = collect_tracked_levels(data)
    mission_snap = persist_mission_state(
        u, aid, levels, phase="list", sync_accounts_daily_bet=sync_accounts_daily_bet
    )

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
                session,
                aid,
                u,
                claimable,
                account_proxy=row_proxy,
                log_prefix="[AUTO-MISSION]",
            )
            if sum(1 for c in claims if c.get("ok")) > 0:
                _sync_balance_to_db(aid, session, label="sau nhận thưởng")
            rep2 = fetch_mission_list(session)
            persist_session(aid, session)
            if rep2.get("ok"):
                data = rep2.get("data") or {}
                levels = collect_tracked_levels(data)
                persist_mission_state(
                    u,
                    aid,
                    levels,
                    phase="after_claim",
                    sync_accounts_daily_bet=sync_accounts_daily_bet,
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
    daily_total = int(
        mission_snap.get("accounts_daily_bet_total") or daily_bet_today_vnd(row)
    )

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

    for c in claims:
        if not c.get("ok"):
            from xoso66_account_errors import maybe_mark_account_loi

            if maybe_mark_account_loi(
                aid, str(c.get("msg") or ""), source="mission/reward"
            ):
                _queue_update(
                    aid,
                    phase="done",
                    pending_claims_json="",
                    last_error=str(c.get("msg") or "")[:500],
                )
                return True

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
        return True

    if only_retry:
        _queue_update(aid, phase="done", pending_claims_json="", last_error="")
        return True

    if had_claimable and claims_ok > 0:
        _queue_update(aid, phase="done", pending_claims_json="", last_error="")
        return True

    if result.get("claim_blocked_by_withdraw"):
        w = result.get("withdraw") or {}
        detail = w.get("withdraw_msg") or w.get("reason") or "chưa rút OK"
        _reschedule_poll(aid, poll_count, f"chờ rút OK — {detail}")
        return True

    if had_claimable and claims_ok == 0:
        if _all_failures_ip_already_claimed(claims):
            msgs = "; ".join(
                dict.fromkeys(str(c.get("msg") or "").strip() for c in claims if not c.get("ok"))
            )[:200]
            print(
                f"[AUTO-MISSION] {u}: dừng — IP/proxy đã nhận thưởng ({msgs}), "
                "không poll lại",
                flush=True,
            )
            _queue_update(
                aid,
                phase="done",
                pending_claims_json="",
                last_error=msgs or "IP đã nhận thưởng",
            )
            return True

        err = "có mức claimable nhưng reward thất bại"
        _queue_update(aid, phase="polling", last_error=err)
        _reschedule_poll(aid, poll_count, err)
        return True

    from xoso66_daily_mission_check import needs_daily_161_bet_poll

    if needs_daily_161_bet_poll(done_bet, daily_total):
        if poll_count >= _poll_max_attempts():
            from xoso66_mission_db import force_daily_done_bet_to_account_total

            synced = force_daily_done_bet_to_account_total(aid)
            print(
                f"[AUTO-MISSION] {u}: hết {poll_count}/{_poll_max_attempts()} lần poll — "
                f"sync tổng cược ngày DB={synced:,} → thử nhận lại",
                flush=True,
            )
            retry = _run_claim_flow(
                aid,
                do_claim=True,
                withdraw_before=True,
                sync_accounts_daily_bet=False,
            )
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
            f"done_bet {done_bet:,} < 888888 và < cược ngày {daily_total:,}",
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
        from xoso66_account_errors import maybe_mark_account_loi

        if maybe_mark_account_loi(aid, err, source="auto-mission"):
            _queue_update(
                aid,
                phase="done",
                pending_claims_json="",
                last_error=err[:500],
            )
            return
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
    from xoso66_account_errors import maybe_mark_account_loi

    if maybe_mark_account_loi(account_id, reason, source="auto-mission"):
        _queue_update(
            account_id,
            phase="done",
            pending_claims_json="",
            last_error=str(reason)[:500],
        )
        return
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
            f"rút ≥ {_min_withdraw_vnd():,}đ, bội {_withdraw_step_vnd():,}đ "
            f"(vd. 300k/400k) → chờ Hoàn tất site rồi mới nhận thưởng",
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
