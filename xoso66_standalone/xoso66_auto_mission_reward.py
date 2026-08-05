# -*- coding: utf-8 -*-
"""
Tự động nhận thưởng nhiệm vụ sau khi acc «Đủ ngày».

Luồng (mỗi acc / ngày VN):
  1. Khi WS ép status → «Đủ ngày» (ngắt WS / đủ cap): hẹn check sau initial_delay_sec.
     Không quét lại toàn bộ acc Đủ ngày lúc khởi động main.
  2. mission/list + lưu DB; nếu có level status=1:
       - sign_list: MINI 17 + điểm danh 22/161
       - mission_list: Cửa MINI GAME (bet_target ≤ tổng cược ngày thật; Cửa 1 = 2.688.000)
       hold_reward_above_min_balance=0 (mặc định):
         balance ≥ min_withdraw_vnd → rút bội withdraw_step_vnd, tối đa max_withdraw_vnd,
         số dư sau rút ≥ min_balance_after_withdraw_vnd (mặc định 50k), mức rút ≥ min_withdraw_vnd,
         poll Hoàn tất, POST reward.
         balance < min → chỉ nhận thưởng (không rút).
       hold_reward_above_min_balance=1:
         balance > min_withdraw_vnd → chỉ refresh số dư + mission/list (không rút, không nhận), xong.
         balance ≤ min_withdraw_vnd → chỉ nhận thưởng (không rút).
  3. Chưa claim được: poll khi (a) done_bet < 888888 VÀ done_bet < tổng cược ngày,
     hoặc (b) daily >= bet_target nhưng cửa vẫn status=0; tối đa poll_max_attempts.
     Trong lúc poll: không ghi đè accounts.daily_bet_total bằng 161.
  4. Hết poll (15/15): sync done_bet_money = tổng cược ngày (accounts + mission) → thử rút + nhận lại.
  5. Claim chỉ khi chuyển status → «Đủ ngày» (đủ cap / ngắt WS):
       - mốc ~890k → nhận điểm danh (sign_list status=1);
       - nâng cap rồi chơi tới ~2690k → Đủ ngày lần nữa → nhận mini game (mission_list).
     Nâng daily_bet_cap trong config không hẹn claim lại.
     claimed_cap ghi theo tiến độ thật (không ghi full config khi mới xong điểm danh).
     Đủ ngày còn room vào lại WS qua fill thường (ws_fill_priority).
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
_MAX_WITHDRAW_VND = 500_000
_MIN_BALANCE_AFTER_WITHDRAW_VND = 50_000


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


_CONSOLIDATE_WITHDRAW_REASON = "đạt ngưỡng rút strategy 3"


def _is_consolidate_withdraw_reason(reason: str) -> bool:
    return str(reason or "").strip() == _CONSOLIDATE_WITHDRAW_REASON


def _is_ws_cap_claim_reason(reason: str) -> bool:
    """Đủ ngày do ngắt WS / đủ cap — vẫn rút dù không có mission claimable."""
    return str(reason or "").strip() in ("ngắt WS", "đủ cap cược ngày")


def _max_task_done_bet(task_levels: list[dict[str, Any]]) -> int:
    best = 0
    for lv in task_levels or []:
        try:
            best = max(best, int(lv.get("done_bet_money") or 0))
        except (TypeError, ValueError):
            continue
    return best


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
        "reason": str(item.get("reason") or "")[:200],
        "claimed_cap_vnd": int(item.get("claimed_cap_vnd") or 0),
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


def _max_withdraw_vnd() -> int:
    return max(0, int(_cfg().get("max_withdraw_vnd", _MAX_WITHDRAW_VND)))


def _min_balance_after_withdraw_vnd() -> int:
    return max(0, int(_cfg().get("min_balance_after_withdraw_vnd", _MIN_BALANCE_AFTER_WITHDRAW_VND)))


def hold_reward_above_min_balance() -> bool:
    """1 = số dư > min_withdraw_vnd thì không rút, không nhận thưởng; ≤ min thì nhận thưởng."""
    return int(_cfg().get("hold_reward_above_min_balance", 0)) == 1


def _sync_balance_to_db(
    account_id: str,
    session: dict,
    *,
    label: str = "",
    force_relogin: bool = False,
) -> float | None:
    from xoso66_session import refresh_account_balance_to_db

    if label:
        pass  # ngữ cảnh (trước/sau rút, v.v.) — dòng log chính từ get_user_balance

    aid = str(account_id).strip()
    rep = refresh_account_balance_to_db(
        aid, session, refresh=True, force_relogin=force_relogin
    )
    if rep.get("ok") and rep.get("balance") is not None:
        return float(rep["balance"])
    return None


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
    max_vnd: int | None = None,
    min_remain_vnd: int | None = None,
) -> int:
    """
    Số tiền rút tự động: bội step_vnd, không vượt số dư / max_withdraw_vnd.
    min_vnd = ngưỡng số dư (balance ≥ mới rút) và mức rút tối thiểu mỗi lần.
    Số dư sau rút phải ≥ min_balance_after_withdraw_vnd (mặc định 50k).
    Trả 0 nếu balance < min_vnd, không rút được ≥ min_vnd, hoặc sau rút còn < min_remain.
    """
    min_v = int(min_vnd if min_vnd is not None else _min_withdraw_vnd())
    step = max(1, int(step_vnd if step_vnd is not None else _withdraw_step_vnd()))
    max_v = int(max_vnd if max_vnd is not None else _max_withdraw_vnd())
    min_remain = int(
        min_remain_vnd if min_remain_vnd is not None else _min_balance_after_withdraw_vnd()
    )
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
    if max_v > 0:
        amt = min(amt, (max_v // step) * step)
    if min_remain > 0 and avail - amt < min_remain:
        amt = ((avail - min_remain) // step) * step
    if amt < min_v or amt <= 0:
        return 0
    if min_remain > 0 and avail - amt < min_remain:
        return 0
    return amt


def _consolidate_withdraw_delay_sec() -> float:
    """Strategy 3 Đủ ngày → chờ trước khi rút (mặc định 420s)."""
    try:
        from xoso66_bet_assign import _auto_bet_cfg, consolidate_withdraw_delay_sec

        return consolidate_withdraw_delay_sec(_auto_bet_cfg(load_config()))
    except Exception:
        return 420.0


def _claimed_cap_from_progress(
    *,
    daily_total: int | float,
    done_bet: int | float,
    cap_vnd: int,
    force_full: bool = False,
) -> int:
    """
    claimed_cap ghi theo tiến độ cược thật — không ghi = full config cap khi mới xong điểm danh (~888k).
    Tránh chặn hẹn Cửa 1 sau này (bug minhhuongkute 03/08).
    """
    cap = max(0, int(cap_vnd or 0))
    if force_full or cap <= 0:
        return cap
    progress = max(0, int(daily_total or 0), int(done_bet or 0))
    step = 10_000
    try:
        ab = load_config().get("auto_bet")
        if isinstance(ab, dict) and ab.get("bet_step_vnd") is not None:
            step = max(1, int(ab.get("bet_step_vnd") or step))
    except Exception:
        pass
    if progress >= max(0, cap - step):
        return cap
    return min(cap, progress)


def _bet_step_vnd() -> int:
    try:
        ab = load_config().get("auto_bet")
        if isinstance(ab, dict) and ab.get("bet_step_vnd") is not None:
            return max(1, int(ab.get("bet_step_vnd") or 10_000))
    except Exception:
        pass
    return 10_000


def schedule_mission_claim(account_id: str, *, reason: str = "") -> bool:
    """
    Hẹn nhận thưởng / rút (sau delay).
    Strategy 3 (đạt ngưỡng rút): delay consolidate_withdraw_delay_sec; cho hẹn lại nếu phase done/failed.
    phase=done cùng ngày: chỉ hẹn lại khi chuyển «Đủ ngày» thật (đã chơi tới mốc cap hiện tại),
    không reclaim chỉ vì nâng daily_bet_cap trong config.
    """
    if not auto_mission_reward_enabled():
        return False
    aid = str(account_id or "").strip()
    if not aid:
        return False
    row = get_account(aid)
    if not row:
        return False
    reason_s = str(reason or "").strip()
    is_s3 = _is_consolidate_withdraw_reason(reason_s)
    is_ws_cap = _is_ws_cap_claim_reason(reason_s)
    delay = _consolidate_withdraw_delay_sec() if is_s3 else _initial_delay_sec()
    vn_day = today_vn_str()
    at = time.time() + delay
    current_cap = _daily_bet_cap_vnd()
    daily_now = int(daily_bet_today_vnd(row) or 0)
    with _QUEUE_LOCK:
        items = _load_queue_map()
        cur = items.get(aid) or {}
        phase = str(cur.get("phase") or "")
        same_day = str(cur.get("vn_day") or "") == vn_day
        claimed_cap = int(cur.get("claimed_cap_vnd") or 0)
        if same_day:
            if is_s3:
                # Đang chạy → không double; done/failed → cho hẹn lại để rút.
                if phase in ("scheduled", "polling", "reward_retry"):
                    return False
            elif phase in ("scheduled", "polling", "reward_retry"):
                return False
            elif phase == "done":
                # Đã xong mốc trước (vd 890k điểm danh). Chỉ hẹn lại khi:
                # chuyển Đủ ngày thật tới mốc cap mới (vd 2690k mini game).
                if not is_ws_cap:
                    return False
                effective_claimed = claimed_cap if claimed_cap > 0 else 900_000
                if current_cap <= effective_claimed:
                    return False
                # Phải đã chơi tới gần cap hiện tại — không reclaim khi mới nâng config.
                if daily_now < max(0, current_cap - _bet_step_vnd()):
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
                "reason": reason_s,
                "claimed_cap_vnd": claimed_cap,
                "updated_at": _now_iso(),
            },
        )
        _save_queue_map(items)
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


def _daily_bet_cap_vnd() -> int:
    cfg = load_config()
    ab = cfg.get("auto_bet")
    if isinstance(ab, dict) and ab.get("daily_bet_cap_vnd") is not None:
        return int(ab.get("daily_bet_cap_vnd") or 890_000)
    gw = cfg.get("game_worker")
    if isinstance(gw, dict) and gw.get("daily_bet_cap_vnd") is not None:
        return int(gw.get("daily_bet_cap_vnd") or 890_000)
    return 890_000


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
    claimed_cap_vnd: int | None = None,
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
        if claimed_cap_vnd is not None:
            nxt["claimed_cap_vnd"] = int(claimed_cap_vnd)
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
    bal = _sync_balance_to_db(
        account_id, session, label="trước rút", force_relogin=False
    )
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
                hold_reward_poll_threshold,
                poll_withdraw_until_confirmed,
                withdraw_confirm_poll_interval_sec,
                withdraw_confirm_poll_max,
            )
            from xoso66_payment_history_db import register_withdraw_submission

            serial = extract_withdraw_serial(wr)
            register_withdraw_submission(
                account_id,
                amt,
                since_ms,
                serial_no=serial,
            )
            out["withdraw_serial"] = serial
            interval = withdraw_confirm_poll_interval_sec()
            max_attempts = withdraw_confirm_poll_max()
            hold_at = min(hold_reward_poll_threshold(), max_attempts)
            print(
                f"[AUTO-MISSION] {u}: chờ rút Hoàn tất trên site — "
                f"poll 1/{max_attempts} (mỗi {interval:.0f}s, hold tại {hold_at}/{max_attempts})"
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
    except Exception as e:
        out["withdraw_ok"] = False
        out["withdraw_msg"] = str(e)
        print(f"[AUTO-MISSION] {u}: rút lỗi — {e}", flush=True)
    return out


def _is_withdraw_turnover_block(msg: str) -> bool:
    """Rút bị chặn vì chưa đủ cược turnover — bỏ rút, vẫn nhận thưởng."""
    m = str(msg or "").strip().lower()
    if not m:
        return False
    return (
        "cần cược thêm" in m
        or "chưa hoàn thành số tiền cần cược" in m
        or "chua hoan thanh so tien can cuoc" in m
    )


def _can_claim_after_withdraw(
    account_id: str,
    withdraw_info: dict[str, Any] | None,
) -> bool:
    """
    True nếu được phép nhận thưởng:
    - Không còn lệnh rút unresolved (đã gửi API, chưa Hoàn tất site).
    - Không bước rút lần này (reward_retry) và không có rút pending.
    - Bỏ rút vì balance < min_withdraw_vnd (và không có rút pending).
    - Rút fail vì chưa đủ cược turnover → bỏ rút, nhận thưởng.
    - Đã rút và lịch sử site xác nhận Hoàn tất (withdraw_confirmed).
    """
    from xoso66_payment_history_db import has_unresolved_withdraw_submission

    if has_unresolved_withdraw_submission(account_id):
        return False
    if withdraw_info is None:
        return True
    if withdraw_info.get("skipped"):
        reason = str(withdraw_info.get("reason") or "")
        return "balance" in reason and "<" in reason
    if not withdraw_info.get("withdraw_ok"):
        detail = str(
            withdraw_info.get("withdraw_msg")
            or withdraw_info.get("reason")
            or ""
        )
        if _is_withdraw_turnover_block(detail):
            u = username_for_log(account_id)
            print(
                f"[AUTO-MISSION] {u}: bỏ rút — {detail} → nhận thưởng không rút",
                flush=True,
            )
            return True
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
    force_login: bool = False,
    ignore_session_ttl: bool = False,
    reason: str = "",
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
    from xoso66_task_mission_reward import (
        collect_claimable_task_levels_for_daily,
        collect_task_levels_from_data,
        needs_task_cua_bet_poll,
    )

    aid = str(account_id).strip()
    row = get_account(aid)
    if not row:
        return {"ok": False, "error": "account not found"}
    u = str(row.get("username") or aid)
    row_proxy = str(row.get("proxy") or "").strip()
    consolidate_withdraw = _is_consolidate_withdraw_reason(reason)
    cap_vnd = _daily_bet_cap_vnd()

    try:
        session = ensure_session(
            aid,
            force_login=force_login,
            ignore_session_ttl=ignore_session_ttl,
        )
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
    db_daily_before = int(daily_bet_today_vnd(row))
    mission_snap = persist_mission_state(
        u, aid, levels, phase="list", sync_accounts_daily_bet=sync_accounts_daily_bet
    )

    task_levels = collect_task_levels_from_data(data)
    task_done_max = _max_task_done_bet(task_levels)
    # max(DB trước sync, sau sync, done_bet Cửa) — tránh lọc Cửa khi 161=888888 ghi sai.
    daily_total_for_claim = max(
        int(mission_snap.get("accounts_daily_bet_total") or 0),
        db_daily_before,
        task_done_max,
    )
    if task_done_max > int(daily_bet_today_vnd(get_account(aid) or {}) or 0):
        from xoso66_accounts_db import set_daily_bet_from_mission_api

        set_daily_bet_from_mission_api(aid, task_done_max)
    claimable = [x for x in levels if x.get("status") == REWARD_CLAIM_STATUS]
    claimable.extend(
        collect_claimable_task_levels_for_daily(data, daily_total_for_claim)
    )
    if only_level_keys:
        claimable = [
            x
            for x in claimable
            if (int(x["mission_id"]), int(x["level_id"])) in only_level_keys
        ]
    # Deduplicate by (mission_id, level_id)
    seen_keys: set[tuple[int, int]] = set()
    deduped: list[dict[str, Any]] = []
    for x in claimable:
        key = (int(x["mission_id"]), int(x["level_id"]))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(x)
    claimable = deduped

    withdraw_info: dict[str, Any] | None = None
    claims: list[dict[str, Any]] = []
    claim_blocked_by_withdraw = False
    high_balance_skip = False
    min_v = _min_withdraw_vnd()

    # Strategy 3 / Đủ ngày: rút kể cả không có mission claimable.
    force_withdraw_no_claim = (
        withdraw_before
        and not (claimable and do_claim)
        and (consolidate_withdraw or _is_ws_cap_claim_reason(reason))
    )
    if force_withdraw_no_claim:
        if hold_reward_above_min_balance():
            bal = _account_balance_vnd(aid, session)
            if bal > min_v:
                high_balance_skip = True
                tag = "strategy 3" if consolidate_withdraw else "Đủ ngày"
                print(
                    f"[AUTO-MISSION] {u}: hold mode — số dư {int(bal):,} > {min_v:,}, "
                    f"bỏ rút ({tag})",
                    flush=True,
                )
        if not high_balance_skip:
            withdraw_info = _maybe_withdraw_before_claim(
                aid, session, row, min_vnd=min_v
            )

    if claimable and do_claim:
        do_withdraw = withdraw_before
        if hold_reward_above_min_balance() and withdraw_before:
            bal = _account_balance_vnd(aid, session)
            if bal > min_v:
                high_balance_skip = True
                print(
                    f"[AUTO-MISSION] {u}: hold mode — số dư {int(bal):,} > {min_v:,}, "
                    "bỏ rút và bỏ nhận thưởng",
                    flush=True,
                )
            else:
                do_withdraw = False
        if not high_balance_skip:
            if do_withdraw:
                withdraw_info = _maybe_withdraw_before_claim(
                    aid, session, row, min_vnd=min_v
                )
            if _can_claim_after_withdraw(aid, withdraw_info):
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
                    task_levels = collect_task_levels_from_data(data)
                    persist_mission_state(
                        u,
                        aid,
                        levels,
                        phase="after_claim",
                        sync_accounts_daily_bet=sync_accounts_daily_bet,
                    )
            else:
                claim_blocked_by_withdraw = True
                from xoso66_payment_history_db import latest_unresolved_withdraw_submission

                pending = latest_unresolved_withdraw_submission(aid)
                if pending:
                    sn = str(pending.get("serial_no") or "").strip()
                    amt = int(pending.get("amount") or 0)
                    print(
                        f"[AUTO-MISSION] {u}: bỏ nhận thưởng — "
                        f"đang chờ rút Hoàn tất"
                        + (f" {amt:,}đ" if amt > 0 else "")
                        + (f" (serial {sn})" if sn else ""),
                        flush=True,
                    )
                else:
                    detail = (
                        (withdraw_info or {}).get("withdraw_msg")
                        or (withdraw_info or {}).get("reason")
                        or "rút thất bại"
                    )
                    print(
                        f"[AUTO-MISSION] {u}: bỏ nhận thưởng — "
                        f"chưa đủ điều kiện rút ({detail})",
                        flush=True,
                    )

    done_bet = _daily_done_bet_from_levels(levels)
    daily_total = daily_total_for_claim
    task_need_poll, task_poll_detail = needs_task_cua_bet_poll(
        task_levels, daily_total
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
        "high_balance_skip": high_balance_skip,
        "consolidate_withdraw": consolidate_withdraw,
        "cap_vnd": cap_vnd,
        "task_need_poll": task_need_poll,
        "task_poll_detail": task_poll_detail,
    }


def _manual_poll_max() -> int:
    raw = _cfg().get("manual_poll_max")
    if raw is not None:
        return max(1, int(raw))
    return min(5, _poll_max_attempts())


def _manual_poll_interval_sec() -> float:
    raw = _cfg().get("manual_poll_interval_sec")
    if raw is not None:
        return max(2.0, float(raw))
    return min(10.0, _poll_interval_sec())


def _manual_claim_should_stop(result: dict[str, Any]) -> bool:
    if not result.get("ok"):
        return True
    if int(result.get("claims_ok") or 0) > 0:
        return True
    if result.get("high_balance_skip") or result.get("claim_blocked_by_withdraw"):
        return True
    if int(result.get("claimable_count") or 0) > 0:
        return True
    return False


def _enrich_manual_claim_result(
    result: dict[str, Any],
    *,
    poll_attempts: int = 0,
    needs_poll: bool = False,
    force_synced: bool = False,
) -> dict[str, Any]:
    out = dict(result)
    snap = out.get("mission_db") or {}
    out["poll_attempts"] = int(poll_attempts)
    out["needs_poll"] = bool(needs_poll)
    out["force_synced"] = bool(force_synced)
    out["daily_status"] = snap.get("daily_status")
    out["mini_current_status"] = snap.get("mini_current_status")
    out["mini_current_day"] = snap.get("mini_current_day")
    return out


def _should_retry_stale_mission_session(result: dict[str, Any]) -> bool:
    if not result.get("ok"):
        return False
    if int(result.get("claimable_count") or 0) > 0:
        return False
    if int(result.get("claims_ok") or 0) > 0:
        return False
    from xoso66_daily_mission_check import (
        DAILY_161_DONE_BET_COMPLETE,
        needs_daily_161_bet_poll,
    )

    done_bet = int(result.get("done_bet_money") or 0)
    daily_total = int(result.get("daily_bet_total") or 0)
    if not needs_daily_161_bet_poll(done_bet, daily_total):
        return False
    return daily_total >= DAILY_161_DONE_BET_COMPLETE


def _retry_stale_mission_session_if_needed(
    account_id: str,
    result: dict[str, Any],
    *,
    do_claim: bool = True,
    withdraw_before: bool = True,
    only_level_keys: set[tuple[int, int]] | None = None,
    sync_accounts_daily_bet: bool | None = None,
    reason: str = "",
) -> dict[str, Any]:
    if not _should_retry_stale_mission_session(result):
        return result
    aid = str(account_id).strip()
    u = username_for_log(aid)
    done_bet = int(result.get("done_bet_money") or 0)
    daily_total = int(result.get("daily_bet_total") or 0)
    print(
        f"[AUTO-MISSION] {u}: session cache lệch — login mới "
        f"(done_bet={done_bet:,}, cược ngày={daily_total:,})",
        flush=True,
    )
    return _run_claim_flow(
        aid,
        do_claim=do_claim,
        withdraw_before=withdraw_before,
        only_level_keys=only_level_keys,
        sync_accounts_daily_bet=sync_accounts_daily_bet,
        force_login=True,
        ignore_session_ttl=True,
        reason=reason,
    )


def _manual_claim_with_poll(
    account_id: str,
    *,
    do_claim: bool = True,
    withdraw_before: bool = True,
    force_login: bool = True,
) -> dict[str, Any]:
    """
    CMS nút Ck — poll done_bet 161 (giống worker) rồi force sync nếu cần.
    """
    from xoso66_daily_mission_check import needs_daily_161_bet_poll
    from xoso66_mission_db import force_daily_done_bet_to_account_total

    aid = str(account_id).strip()
    u = username_for_log(aid)
    poll_attempts = 0
    needs_poll = False
    force_synced = False

    if force_login:
        print(
            f"[AUTO-MISSION] {u}: session trước mission/list (Ck)",
            flush=True,
        )

    result = _run_claim_flow(
        aid,
        do_claim=do_claim,
        withdraw_before=withdraw_before,
        force_login=False,
    )
    if _manual_claim_should_stop(result):
        return _enrich_manual_claim_result(result, poll_attempts=poll_attempts)

    done_bet = int(result.get("done_bet_money") or 0)
    daily_total = int(result.get("daily_bet_total") or 0)
    if not needs_daily_161_bet_poll(done_bet, daily_total):
        return _enrich_manual_claim_result(result, poll_attempts=poll_attempts)

    needs_poll = True
    max_poll = _manual_poll_max()
    interval = _manual_poll_interval_sec()
    print(
        f"[AUTO-MISSION] {u}: Ck poll — done_bet {done_bet:,} < cược ngày {daily_total:,} "
        f"({max_poll} lần, mỗi {interval:.0f}s)",
        flush=True,
    )

    while poll_attempts < max_poll:
        if stopping():
            break
        poll_attempts += 1
        time.sleep(interval)
        result = _run_claim_flow(aid, do_claim=do_claim, withdraw_before=withdraw_before)
        if _manual_claim_should_stop(result):
            return _enrich_manual_claim_result(
                result,
                poll_attempts=poll_attempts,
                needs_poll=needs_poll,
                force_synced=force_synced,
            )
        done_bet = int(result.get("done_bet_money") or 0)
        daily_total = int(result.get("daily_bet_total") or 0)
        if not needs_daily_161_bet_poll(done_bet, daily_total):
            break

    synced = force_daily_done_bet_to_account_total(aid)
    force_synced = True
    print(
        f"[AUTO-MISSION] {u}: Ck hết poll — sync done_bet 161 = {synced:,} → thử nhận lại",
        flush=True,
    )
    result = _run_claim_flow(
        aid,
        do_claim=do_claim,
        withdraw_before=withdraw_before,
        sync_accounts_daily_bet=False,
    )
    return _enrich_manual_claim_result(
        result,
        poll_attempts=poll_attempts,
        needs_poll=needs_poll,
        force_synced=force_synced,
    )


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


def _mark_queue_done(
    aid: str,
    *,
    last_error: str = "",
    claimed_cap_vnd: int | None = None,
) -> None:
    cap = int(claimed_cap_vnd) if claimed_cap_vnd is not None else _daily_bet_cap_vnd()
    _queue_update(
        aid,
        phase="done",
        pending_claims_json="",
        last_error=last_error,
        claimed_cap_vnd=cap,
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
    cap_vnd = int(result.get("cap_vnd") or _daily_bet_cap_vnd())
    task_need_poll = bool(result.get("task_need_poll"))
    task_poll_detail = str(result.get("task_poll_detail") or "")
    progress_cap = _claimed_cap_from_progress(
        daily_total=daily_total, done_bet=done_bet, cap_vnd=cap_vnd
    )

    for c in claims:
        if not c.get("ok"):
            from xoso66_account_errors import maybe_mark_account_loi

            if maybe_mark_account_loi(
                aid, str(c.get("msg") or ""), source="mission/reward"
            ):
                _mark_queue_done(
                    aid,
                    last_error=str(c.get("msg") or "")[:500],
                    claimed_cap_vnd=cap_vnd,
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
        _mark_queue_done(
            aid,
            last_error=f"bỏ qua rate-limit: {levels}",
            claimed_cap_vnd=cap_vnd,
        )
        return True

    if only_retry:
        _mark_queue_done(aid, claimed_cap_vnd=progress_cap)
        return True

    if had_claimable and claims_ok > 0 and not task_need_poll:
        _mark_queue_done(aid, claimed_cap_vnd=progress_cap)
        return True

    if result.get("claim_blocked_by_withdraw"):
        w = result.get("withdraw") or {}
        detail = w.get("withdraw_msg") or w.get("reason") or "chưa rút OK"
        _reschedule_poll(aid, poll_count, f"chờ rút OK — {detail}")
        return True

    if result.get("high_balance_skip"):
        min_v = _min_withdraw_vnd()
        print(
            f"[AUTO-MISSION] {u}: xong — hold mode, số dư > {min_v:,} "
            "(không rút, không nhận thưởng)",
            flush=True,
        )
        _mark_queue_done(
            aid,
            last_error=f"hold mode: số dư > {min_v:,}",
            claimed_cap_vnd=cap_vnd,
        )
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
            _mark_queue_done(
                aid,
                last_error=msgs or "IP đã nhận thưởng",
                claimed_cap_vnd=cap_vnd,
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

    if task_need_poll:
        if poll_count >= _poll_max_attempts():
            print(
                f"[AUTO-MISSION] {u}: hết {poll_count}/{_poll_max_attempts()} lần poll cửa — "
                f"{task_poll_detail or 'chờ status=1'} → thử nhận lại",
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
                last_error=str(retry.get("error") or "poll cửa xong, vẫn chưa nhận được"),
            )
            return True

        _reschedule_poll(
            aid,
            poll_count,
            task_poll_detail or f"chờ cửa status=1 (cược ngày {daily_total:,}, cap {cap_vnd:,})",
        )
        return True

    if not had_claimable:
        w = result.get("withdraw") or {}
        if result.get("consolidate_withdraw"):
            if (
                w
                and not w.get("skipped")
                and w.get("withdraw_ok") is False
                and not result.get("high_balance_skip")
            ):
                detail = w.get("withdraw_msg") or w.get("reason") or "chưa rút OK"
                _reschedule_poll(aid, poll_count, f"strategy 3 chờ rút OK — {detail}")
                return True
            detail = ""
            if w.get("withdraw_ok"):
                detail = f" — đã rút {int(w.get('withdraw_amount') or 0):,}đ"
            elif w:
                detail = (
                    f" — rút: {w.get('withdraw_msg') or w.get('reason') or 'không rút'}"
                )
            print(
                f"[AUTO-MISSION] {u}: strategy 3 — không có mission claimable"
                f"{detail} (done_bet={done_bet:,}, cược ngày={daily_total:,})",
                flush=True,
            )
        else:
            print(
                f"[AUTO-MISSION] {u}: không có mức status=1 — coi xong "
                f"(done_bet={done_bet:,}, cược ngày={daily_total:,}, "
                f"claimed_cap={progress_cap:,}/{cap_vnd:,})",
                flush=True,
            )
        _mark_queue_done(aid, claimed_cap_vnd=progress_cap)
        return True

    _mark_queue_done(aid, claimed_cap_vnd=progress_cap)
    return True


def _process_queue_row(qrow: dict[str, Any]) -> None:
    aid = str(qrow.get("account_id") or "").strip()
    if not aid:
        return
    poll_count = int(qrow.get("poll_count") or 0)
    reward_retry_count = int(qrow.get("reward_retry_count") or 0)
    phase = str(qrow.get("phase") or "")
    pending_keys = _pending_claim_keys(str(qrow.get("pending_claims_json") or ""))
    reason = str(qrow.get("reason") or "")
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
            reason=reason,
        )
        if not is_reward_retry:
            result = _retry_stale_mission_session_if_needed(
                aid,
                result,
                do_claim=True,
                withdraw_before=True,
                reason=reason,
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
        hold = hold_reward_above_min_balance()
        mode = (
            f"hold mode (số dư > {_min_withdraw_vnd():,} → bỏ rút/nhận; ≤ → nhận thưởng)"
            if hold
            else (
                f"rút ≥ {_min_withdraw_vnd():,}đ, bội {_withdraw_step_vnd():,}đ, "
                f"tối đa {_max_withdraw_vnd():,}đ/lần, còn lại ≥ {_min_balance_after_withdraw_vnd():,}đ "
                f"→ chờ Hoàn tất site rồi mới nhận thưởng"
            )
        )
        print(
            f"[AUTO-MISSION] Worker: delay đầu {_initial_delay_sec():.0f}s, "
            f"poll {_poll_interval_sec():.0f}s × {_poll_max_attempts()}, "
            f"rate-limit retry {_reward_retry_delay_sec():.0f}s × {_reward_retry_max()}, "
            f"{mode}",
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


_MANUAL_CLAIM_LOCK = threading.Lock()


def run_manual_auto_mission_claim(
    account_ids: list[str],
    *,
    do_claim: bool = True,
    withdraw_before: bool = True,
) -> dict[str, Any]:
    """
    CMS nút Ck — cùng luồng worker auto-mission (rút + nhận + log [AUTO-MISSION]).
    """
    if not _MANUAL_CLAIM_LOCK.acquire(blocking=False):
        return {
            "ok": False,
            "busy": True,
            "error": "Đang có lượt auto-mission (Ck) khác — chờ xong rồi thử",
            "results": [],
        }
    try:
        aids = [str(x).strip() for x in (account_ids or []) if str(x).strip()]
        if not aids:
            return {"ok": False, "error": "account_ids bắt buộc", "results": []}
        results: list[dict[str, Any]] = []
        for aid in aids:
            try:
                results.append(
                    _manual_claim_with_poll(
                        aid,
                        do_claim=do_claim,
                        withdraw_before=withdraw_before,
                        force_login=True,
                    )
                )
            except Exception as e:
                results.append({"ok": False, "account_id": aid, "error": str(e)})
        ok_n = sum(1 for r in results if r.get("ok"))
        fail_n = len(results) - ok_n
        claimed_n = sum(int(r.get("claims_ok") or 0) for r in results)
        return {
            "ok": fail_n == 0,
            "total": len(results),
            "ok_count": ok_n,
            "fail_count": fail_n,
            "claims_ok_total": claimed_n,
            "results": results,
        }
    finally:
        _MANUAL_CLAIM_LOCK.release()


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
