# -*- coding: utf-8 -*-
"""
Đặt lại auto_bet.daily_bet_cap_vnd hàng ngày (giờ VN).

Mặc định: 00:05 → 895000 (mốc điểm danh). Trong ngày có thể nâng cap
(VD 2695000 Cửa 1 mini game); sau nửa đêm scheduler kéo về lại.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from xoso66_config_util import load_config, save_user_config_value
from xoso66_paths import cms_game_data_dir
from xoso66_shutdown import stopping
from xoso66_time_util import now_vn, today_vn_str

_RUN_LOCK = threading.Lock()
_STATE_FILE = Path(cms_game_data_dir()) / "daily_bet_cap_reset_state.json"
_CONFIG_PATH = ("auto_bet", "daily_bet_cap_vnd")


def _cfg() -> dict[str, Any]:
    raw = load_config().get("daily_bet_cap_reset")
    return raw if isinstance(raw, dict) else {}


def daily_bet_cap_reset_enabled() -> bool:
    return bool(_cfg().get("enabled", True))


def _schedule_hour() -> int:
    return int(_cfg().get("hour", 0))


def _schedule_minute() -> int:
    return int(_cfg().get("minute", 5))


def _target_cap_vnd() -> int:
    return int(_cfg().get("value_vnd", 895_000))


def _worker_tick_sec() -> float:
    return float(_cfg().get("worker_tick_sec", 30))


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


def reset_ran_today_vn() -> bool:
    day = today_vn_str()
    st = _load_state()
    return str(st.get("vn_day") or "") == day and bool(st.get("reset_ran"))


def _mark_reset_done(cap_vnd: int) -> None:
    from datetime import datetime, timezone

    _save_state(
        {
            "vn_day": today_vn_str(),
            "reset_ran": True,
            "cap_vnd": int(cap_vnd),
            "done_at": datetime.now(timezone.utc).isoformat(),
        }
    )


def _due_for_daily_reset() -> bool:
    if reset_ran_today_vn():
        return False
    now = now_vn()
    target = now.replace(
        hour=_schedule_hour(),
        minute=_schedule_minute(),
        second=0,
        microsecond=0,
    )
    return now >= target


def run_daily_bet_cap_reset(*, reason: str = "scheduled") -> bool:
    """Ghi daily_bet_cap_vnd về value_vnd. False nếu đã chạy hôm nay / busy / lỗi ghi."""
    if not _RUN_LOCK.acquire(blocking=False):
        print("[CAP-RESET] Đang chạy reset khác — bỏ qua", flush=True)
        return False
    try:
        if reset_ran_today_vn():
            return False
        cap = _target_cap_vnd()
        ab = load_config().get("auto_bet")
        prev = None
        if isinstance(ab, dict) and ab.get("daily_bet_cap_vnd") is not None:
            try:
                prev = int(ab.get("daily_bet_cap_vnd"))
            except (TypeError, ValueError):
                prev = None
        if prev is not None and prev == cap:
            _mark_reset_done(cap)
            print(
                f"[CAP-RESET] Cap đã = {cap:,} — đánh dấu đã reset ({reason})",
                flush=True,
            )
            return True
        if not save_user_config_value(_CONFIG_PATH, cap):
            print(
                f"[CAP-RESET] Không ghi được daily_bet_cap_vnd={cap:,}",
                flush=True,
            )
            return False
        _mark_reset_done(cap)
        prev_s = f"{prev:,}" if prev is not None else "?"
        print(
            f"[CAP-RESET] daily_bet_cap_vnd {prev_s} → {cap:,} ({reason})",
            flush=True,
        )
        return True
    except Exception as e:
        print(f"[CAP-RESET] Lỗi: {e}", flush=True)
        return False
    finally:
        _RUN_LOCK.release()


def worker_daily_bet_cap_reset_loop(*, quiet: bool = False) -> None:
    if not daily_bet_cap_reset_enabled():
        return
    tick = _worker_tick_sec()
    if not quiet:
        print(
            f"[CAP-RESET] Worker: {_schedule_hour():02d}:{_schedule_minute():02d} "
            f"giờ VN → daily_bet_cap_vnd={_target_cap_vnd():,}",
            flush=True,
        )
    while not stopping():
        try:
            if _due_for_daily_reset():
                run_daily_bet_cap_reset(
                    reason=f"{_schedule_hour():02d}:{_schedule_minute():02d} VN"
                )
        except Exception as e:
            if not stopping():
                print(f"[CAP-RESET] Lỗi vòng quét: {e}", flush=True)
        for _ in range(max(1, int(tick))):
            if stopping():
                break
            time.sleep(1)
    print("[CAP-RESET] Worker đã dừng.", flush=True)


def start_daily_bet_cap_reset_thread(*, quiet: bool = False) -> threading.Thread | None:
    if not daily_bet_cap_reset_enabled():
        return None
    t = threading.Thread(
        target=worker_daily_bet_cap_reset_loop,
        kwargs={"quiet": quiet},
        daemon=False,
        name="xoso66-cap-reset",
    )
    t.start()
    return t
