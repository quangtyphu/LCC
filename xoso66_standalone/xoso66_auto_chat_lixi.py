# -*- coding: utf-8 -*-
"""
Auto nhận lì xì phòng chat — từ phút :02 mỗi giờ VN (bao chat thường rơi :02, không phải :01).

Bật: auto_chat_lixi.enabled=true. Song song parallel (mặc định 5).

Discover: **1 nick** random (session sẵn) + getChatLog.red_notify — không thấy bao thì hẹn phút sau.
Lặp đến ``retry_through_minute`` (mặc định :04) trong cùng giờ.
Grab: thứ tự nick random, song song ``parallel``. ``max_fail_streak``: số lần
fail liên tiếp (bỏ qua skip_session) rồi dừng — ``0`` = không giới hạn (chạy hết acc).
Một lần nhận thành công đưa streak về 0. Nick thiếu session không tính.
Một nick gặp code 1054 (máy chủ bận) thì thử lại tối đa 3 lần rồi mới ghi kết quả.
"""

from __future__ import annotations

import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from xoso66_chat_lixi_store import (
    init_chat_lixi_table,
    insert_claim,
    vn_hour_key,
)
from xoso66_config_util import load_config
from xoso66_paths import cms_game_data_dir
from xoso66_shutdown import stopping
from xoso66_time_util import now_vn

_RUN_LOCK = threading.Lock()
_STATE_FILE = Path(cms_game_data_dir()) / "chat_lixi_hourly_run_state.json"

# 1054 «Máy chủ đang bận, vui lòng thử lại sau» — gọi lại thêm tối đa 3 lần.
_BUSY_CODE = 1054
_BUSY_RETRY_MAX = 3
_BUSY_RETRY_DELAY_SEC = 1.0


def _cfg() -> dict[str, Any]:
    raw = load_config().get("auto_chat_lixi")
    return raw if isinstance(raw, dict) else {}


def auto_chat_lixi_enabled() -> bool:
    return bool(_cfg().get("enabled", False))


def _schedule_minute() -> int:
    return max(0, min(59, int(_cfg().get("minute", 2))))


def _retry_through_minute() -> int:
    """Phút cuối cùng trong giờ vẫn thử discover nếu các lần trước no_red."""
    start = _schedule_minute()
    end = int(_cfg().get("retry_through_minute", start + 2))
    return max(start, min(59, end))


def _discover_minutes() -> tuple[int, ...]:
    start = _schedule_minute()
    end = _retry_through_minute()
    return tuple(range(start, end + 1))


def _parallel() -> int:
    return max(1, int(_cfg().get("parallel", 5)))


def _max_fail_streak() -> int:
    """0 = không giới hạn; >0 = dừng grab sau N lần không thành công liên tiếp."""
    try:
        return max(0, int(_cfg().get("max_fail_streak", 5)))
    except (TypeError, ValueError):
        return 5


def _worker_tick_sec() -> float:
    return float(_cfg().get("worker_tick_sec", 15))


def _profile() -> str:
    return str(_cfg().get("profile") or "android_app").strip() or "android_app"


def _is_server_busy_1054(result: dict[str, Any]) -> bool:
    try:
        return int(result.get("code")) == _BUSY_CODE
    except (TypeError, ValueError):
        return False


def _load_state() -> dict[str, Any]:
    try:
        raw = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_state(payload: dict[str, Any]) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def slot_already_ran(hour_key: str | None = None) -> bool:
    key = hour_key or vn_hour_key()
    st = _load_state()
    return str(st.get("vn_hour") or "") == key and bool(st.get("ran"))


def _hour_state(hour_key: str) -> dict[str, Any]:
    st = _load_state()
    if str(st.get("vn_hour") or "") != hour_key:
        return {}
    return st


def _tried_minutes_for_hour(hour_key: str) -> set[int]:
    st = _hour_state(hour_key)
    raw = st.get("tried_minutes")
    if not isinstance(raw, list):
        return set()
    out: set[int] = set()
    for x in raw:
        try:
            out.add(int(x))
        except (TypeError, ValueError):
            continue
    return out


def _mark_hour_progress(
    hour_key: str,
    *,
    minute: int,
    finished: bool,
    note: str = "",
) -> None:
    from datetime import datetime, timezone

    st = _load_state()
    tried: list[int] = []
    if str(st.get("vn_hour") or "") == hour_key and isinstance(st.get("tried_minutes"), list):
        tried = [int(x) for x in st["tried_minutes"] if x is not None]
    if minute not in tried:
        tried.append(minute)
    payload: dict[str, Any] = {
        "vn_hour": hour_key,
        "ran": finished,
        "tried_minutes": sorted(tried),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if note:
        payload["last_note"] = note
    if finished:
        payload["finished_at"] = payload["updated_at"]
    _save_state(payload)


def _mark_slot_ran(hour_key: str, *, minute: int | None = None, note: str = "") -> None:
    _mark_hour_progress(
        hour_key,
        minute=minute if minute is not None else _retry_through_minute(),
        finished=True,
        note=note or "done",
    )


def _due_for_hourly_run() -> bool:
    now = now_vn()
    minute = int(now.minute)
    if minute not in _discover_minutes():
        return False
    hour_key = vn_hour_key(now)
    if slot_already_ran(hour_key):
        return False
    return minute not in _tried_minutes_for_hour(hour_key)


def _session_ready(acc: dict[str, Any]) -> bool:
    sj = acc.get("session_json") if isinstance(acc.get("session_json"), dict) else {}
    token = str(sj.get("form_token") or "").strip()
    proxy = str(acc.get("proxy") or sj.get("proxy") or "").strip()
    return bool(token and proxy)


def _discover_live_red(session: dict[str, Any]) -> dict[str, Any] | None:
    from xoso66_chat_red_packet import discover_live_chat_red
    from xoso66_red_packet import apply_client_profile

    apply_client_profile(session, _profile())
    return discover_live_chat_red(session, join_chat=True)


def _grab_one(
    acc: dict[str, Any],
    *,
    red_id: int,
    vn_hour: str,
    profile: str,
) -> dict[str, Any]:
    from xoso66_accounts_db import account_to_session_dict
    from xoso66_chat_red_packet import grab_chat_red

    aid = str(acc.get("id") or "").strip()
    user = str(acc.get("username") or "").strip()
    out: dict[str, Any] = {
        "account_id": aid,
        "username": user,
        "red_id": red_id,
    }
    if not _session_ready(acc):
        insert_claim(
            account_id=aid,
            username=user,
            red_id=red_id,
            money=0,
            status="skip_session",
            msg="thieu form_token/proxy",
            vn_hour=vn_hour,
        )
        out["status"] = "skip_session"
        return out
    try:
        session = account_to_session_dict(acc)
        g = grab_chat_red(session, red_id, client_profile=profile)
        for retry_n in range(1, _BUSY_RETRY_MAX + 1):
            if not _is_server_busy_1054(g) or stopping():
                break
            print(
                f"[AUTO-CHAT-LIXI] {user}: code 1054 — "
                f"thử lại {retry_n}/{_BUSY_RETRY_MAX}",
                flush=True,
            )
            time.sleep(_BUSY_RETRY_DELAY_SEC)
            if stopping():
                break
            g = grab_chat_red(
                session,
                red_id,
                client_profile=profile,
                join_chat=False,
            )
        ok = bool(g.get("ok"))
        money = int(g.get("money") or 0) if ok else 0
        status = "success" if ok else "fail"
        msg = str(g.get("msg") or "")
        insert_claim(
            account_id=aid,
            username=user,
            red_id=red_id,
            money=money,
            status=status,
            code=g.get("code") if g.get("code") is not None else None,
            msg=msg,
            vn_hour=vn_hour,
        )
        out.update(
            {
                "status": status,
                "ok": ok,
                "code": g.get("code"),
                "msg": g.get("msg"),
                "money": money,
            }
        )
    except Exception as e:
        insert_claim(
            account_id=aid,
            username=user,
            red_id=red_id,
            money=0,
            status="error",
            msg=f"{type(e).__name__}: {e}",
            vn_hour=vn_hour,
        )
        out["status"] = "error"
        out["msg"] = f"{type(e).__name__}: {e}"
    return out


def _shuffle_accounts(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Thứ tự random mỗi lần check/nhận (DB mặc định ORDER BY id)."""
    out = list(accounts)
    random.shuffle(out)
    return out


def _discover_live_one(
    accounts: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    """
    Một nick (đầu danh sách đã random, có session) — đủ để biết có bao live hay không.
    Trả (live|None, discover_user).
    """
    from xoso66_accounts_db import account_to_session_dict

    ready = [a for a in accounts if _session_ready(a)]
    if not ready:
        print("[AUTO-CHAT-LIXI] Discover: không có nick session sẵn", flush=True)
        return None, ""
    acc = ready[0]
    user = str(acc.get("username") or acc.get("id") or "")
    try:
        session = account_to_session_dict(acc)
        live = _discover_live_red(session)
        if live:
            print(
                f"[AUTO-CHAT-LIXI] Discover ok nick={user} "
                f"red_id={live.get('red_id')} "
                f"left={live.get('time_interval')}s source={live.get('source')}",
                flush=True,
            )
            return live, user
        print(f"[AUTO-CHAT-LIXI] Discover no_red nick={user}", flush=True)
        return None, user
    except Exception as e:
        print(f"[AUTO-CHAT-LIXI] Discover lỗi {user}: {e}", flush=True)
        return None, user


def _grab_batch(
    accounts: list[dict[str, Any]],
    *,
    red_id: int,
    vn_hour: str,
    profile: str,
    workers: int,
) -> list[dict[str, Any]]:
    """Grab một đợt; kết quả theo đúng thứ tự ``accounts`` (không theo as_completed)."""
    if not accounts:
        return []
    if workers <= 1 or len(accounts) <= 1:
        out: list[dict[str, Any]] = []
        for acc in accounts:
            if stopping():
                break
            out.append(
                _grab_one(acc, red_id=red_id, vn_hour=vn_hour, profile=profile)
            )
        return out

    indexed: list[tuple[int, dict[str, Any]]] = list(enumerate(accounts))
    results_by_idx: dict[int, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(workers, len(accounts))) as pool:
        futs = {
            pool.submit(
                _grab_one,
                acc,
                red_id=red_id,
                vn_hour=vn_hour,
                profile=profile,
            ): idx
            for idx, acc in indexed
        }
        for fut in as_completed(futs):
            idx = futs[fut]
            acc = accounts[idx]
            try:
                results_by_idx[idx] = fut.result()
            except Exception as e:
                insert_claim(
                    account_id=str(acc.get("id") or ""),
                    username=str(acc.get("username") or ""),
                    red_id=red_id,
                    status="error",
                    msg=f"{type(e).__name__}: {e}",
                    vn_hour=vn_hour,
                )
                results_by_idx[idx] = {
                    "account_id": str(acc.get("id") or ""),
                    "username": str(acc.get("username") or ""),
                    "status": "error",
                    "msg": str(e),
                }
    return [results_by_idx[i] for i in range(len(accounts)) if i in results_by_idx]


def run_hourly_chat_lixi_batch(*, reason: str = "scheduled") -> dict[str, Any] | None:
    """Chạy 1 slot giờ — trả None nếu busy / không due / đã chạy."""
    if not _RUN_LOCK.acquire(blocking=False):
        print("[AUTO-CHAT-LIXI] Đang chạy batch khác — bỏ qua", flush=True)
        return None
    try:
        init_chat_lixi_table()
        now = now_vn()
        hour_key = vn_hour_key(now)
        run_minute = int(now.minute)
        if slot_already_ran(hour_key):
            return None
        if run_minute in _tried_minutes_for_hour(hour_key):
            return None

        from xoso66_accounts_db import list_accounts

        # Random thứ tự mọi lần check + nhận (tránh luôn nick id nhỏ trước).
        accounts = _shuffle_accounts(list_accounts())
        profile = _profile()
        parallel = _parallel()
        fail_limit = _max_fail_streak()
        unlimited_fails = fail_limit == 0

        live, discover_user = _discover_live_one(accounts)

        if not live:
            final_attempt = run_minute >= _retry_through_minute()
            if final_attempt:
                insert_claim(
                    account_id="_system",
                    username=discover_user or "_",
                    red_id=None,
                    money=0,
                    status="no_red",
                    msg="khong co bao time_interval > 0",
                    vn_hour=hour_key,
                )
                _mark_slot_ran(hour_key, minute=run_minute, note="no_red_final")
                print(
                    f"[AUTO-CHAT-LIXI] {hour_key} :{run_minute:02d} ({reason}): "
                    f"không có bao (1 nick) — kết thúc slot",
                    flush=True,
                )
            else:
                _mark_hour_progress(
                    hour_key,
                    minute=run_minute,
                    finished=False,
                    note="no_red_retry",
                )
                print(
                    f"[AUTO-CHAT-LIXI] {hour_key} :{run_minute:02d} ({reason}): "
                    f"chưa thấy bao (1 nick) — thử lại phút sau "
                    f"(đến :{_retry_through_minute():02d})",
                    flush=True,
                )
            return {
                "ok": True,
                "vn_hour": hour_key,
                "red_id": None,
                "total": len(accounts),
                "success": 0,
                "skipped": "no_red",
                "retry": not final_attempt,
            }

        red_id = int(live["red_id"])
        print(
            f"[AUTO-CHAT-LIXI] {hour_key} ({reason}): red_id={red_id} "
            f"left={live.get('time_interval')}s — {len(accounts)} nick random, "
            f"parallel={parallel}, max_fail_streak={fail_limit or '∞'}, profile={profile}",
            flush=True,
        )

        results: list[dict[str, Any]] = []
        workers = 1 if len(accounts) <= 1 else max(1, min(parallel, len(accounts)))
        # Bất kỳ lần không thành công nào cũng tính. Thành công đưa streak về 0.
        # Đợt sau chỉ lấy đúng số nick còn thiếu để chạm ngưỡng, tránh nhận thừa.
        fail_streak = 0
        stopped_fail_streak = False
        idx = 0
        while idx < len(accounts):
            if stopping():
                break
            if unlimited_fails:
                take = min(workers, len(accounts) - idx)
            else:
                room = fail_limit - fail_streak
                if room <= 0:
                    stopped_fail_streak = True
                    break
                take = min(workers, room, len(accounts) - idx)
            batch = accounts[idx : idx + take]
            batch_rows = _grab_batch(
                batch,
                red_id=red_id,
                vn_hour=hour_key,
                profile=profile,
                workers=take,
            )
            results.extend(batch_rows)

            for row in batch_rows:
                if row.get("status") == "skip_session":
                    continue
                if row.get("status") == "success":
                    fail_streak = 0
                else:
                    fail_streak += 1
                    if not unlimited_fails and fail_streak >= fail_limit:
                        stopped_fail_streak = True
                        break

            idx += take
            if stopped_fail_streak:
                rest_n = len(accounts) - idx
                if rest_n > 0:
                    print(
                        f"[AUTO-CHAT-LIXI] Dừng: {fail_limit} lần không thành công "
                        f"liên tiếp — bỏ {rest_n} nick còn lại (không ghi DB)",
                        flush=True,
                    )
                break

        success = sum(1 for r in results if r.get("status") == "success")
        money = sum(int(r.get("money") or 0) for r in results if r.get("status") == "success")
        _mark_slot_ran(hour_key, minute=run_minute, note="grab_done")
        print(
            f"[AUTO-CHAT-LIXI] Xong {hour_key}: success={success}/{len(results)} "
            f"tổng +{money:,}"
            + (" (stopped_fail_streak)" if stopped_fail_streak else ""),
            flush=True,
        )
        return {
            "ok": True,
            "vn_hour": hour_key,
            "red_id": red_id,
            "total": len(results),
            "success": success,
            "total_money": money,
            "stopped_fail_streak": stopped_fail_streak,
            "results": results,
        }
    except Exception as e:
        print(f"[AUTO-CHAT-LIXI] Lỗi batch: {e}", flush=True)
        return {"ok": False, "error": str(e)}
    finally:
        _RUN_LOCK.release()


def worker_auto_chat_lixi_loop(*, quiet: bool = False) -> None:
    tick = _worker_tick_sec()
    if not quiet:
        en = "BẬT" if auto_chat_lixi_enabled() else "TẮT"
        mins = _discover_minutes()
        window = f":{mins[0]:02d}" if len(mins) == 1 else f":{mins[0]:02d}-:{mins[-1]:02d}"
        print(
            f"[AUTO-CHAT-LIXI] Worker: mỗi giờ {window} VN, "
            f"parallel {_parallel()} — hiện {en}",
            flush=True,
        )
    while not stopping():
        try:
            if auto_chat_lixi_enabled() and _due_for_hourly_run():
                run_hourly_chat_lixi_batch(reason="hourly discover VN")
        except Exception as e:
            if not stopping():
                print(f"[AUTO-CHAT-LIXI] Lỗi vòng quét: {e}", flush=True)
        for _ in range(max(1, int(tick))):
            if stopping():
                break
            time.sleep(1)
    print("[AUTO-CHAT-LIXI] Worker đã dừng.", flush=True)


def start_auto_chat_lixi_thread(*, quiet: bool = False) -> threading.Thread:
    """Luôn start — worker tự no-op khi enabled=false (CMS toggle không cần restart để tắt)."""
    t = threading.Thread(
        target=worker_auto_chat_lixi_loop,
        kwargs={"quiet": quiet},
        daemon=False,
        name="xoso66-auto-chat-lixi",
    )
    t.start()
    return t
