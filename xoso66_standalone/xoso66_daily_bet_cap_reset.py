# -*- coding: utf-8 -*-
"""
Đặt lại auto_bet hàng ngày (giờ VN): daily_bet_cap_vnd, min_jackpot_vnd, side_total_low_vnd,
và bật auto_bet.enabled (mặc định true dù đang false).

Mặc định: 00:05 → cap 895000 (mốc điểm danh), min_jackpot 2 tỷ, side_total_low 50k.
Trong ngày có thể nâng cap/ngưỡng hũ; sau nửa đêm scheduler kéo về lại.
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
_CAP_PATH = ("auto_bet", "daily_bet_cap_vnd")
_MIN_JP_PATH = ("auto_bet", "min_jackpot_vnd")
_SIDE_LOW_PATH = ("auto_bet", "side_total_low_vnd")
_ENABLED_PATH = ("auto_bet", "enabled")


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


def _target_min_jackpot_vnd() -> int:
    return int(_cfg().get("min_jackpot_vnd", 2_000_000_000))


def _target_side_total_low_vnd() -> int:
    return int(_cfg().get("side_total_low_vnd", 50_000))


def _target_auto_bet_enabled() -> bool:
    return bool(_cfg().get("auto_bet_enabled", True))


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


def _mark_reset_done(
    cap_vnd: int,
    *,
    min_jackpot_vnd: int,
    side_total_low_vnd: int,
) -> None:
    from datetime import datetime, timezone

    _save_state(
        {
            "vn_day": today_vn_str(),
            "reset_ran": True,
            "cap_vnd": int(cap_vnd),
            "min_jackpot_vnd": int(min_jackpot_vnd),
            "side_total_low_vnd": int(side_total_low_vnd),
            "done_at": datetime.now(timezone.utc).isoformat(),
        }
    )


def _auto_bet_int(ab: dict[str, Any] | None, key: str) -> int | None:
    if not isinstance(ab, dict) or ab.get(key) is None:
        return None
    try:
        return int(ab.get(key))
    except (TypeError, ValueError):
        return None


def _auto_bet_at_daily_targets(ab: dict[str, Any] | None) -> bool:
    targets = (
        ("daily_bet_cap_vnd", _target_cap_vnd()),
        ("min_jackpot_vnd", _target_min_jackpot_vnd()),
        ("side_total_low_vnd", _target_side_total_low_vnd()),
    )
    for key, target in targets:
        prev = _auto_bet_int(ab, key)
        if prev is None or prev != target:
            return False
    if _target_auto_bet_enabled():
        if not isinstance(ab, dict) or not bool(ab.get("enabled")):
            return False
    return True


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
    """Ghi cap / min_jackpot / side_total_low về mốc daily_bet_cap_reset. False nếu đã chạy hôm nay / busy / lỗi ghi."""
    if not _RUN_LOCK.acquire(blocking=False):
        print("[CAP-RESET] Đang chạy reset khác — bỏ qua", flush=True)
        return False
    try:
        if reset_ran_today_vn():
            return False
        cap = _target_cap_vnd()
        min_jp = _target_min_jackpot_vnd()
        side_low = _target_side_total_low_vnd()
        ab = load_config().get("auto_bet")
        ab_dict = ab if isinstance(ab, dict) else None
        if _auto_bet_at_daily_targets(ab_dict):
            _mark_reset_done(
                cap,
                min_jackpot_vnd=min_jp,
                side_total_low_vnd=side_low,
            )
            print(
                f"[CAP-RESET] Cap/min_jackpot/side_total_low đã đúng mốc — "
                f"đánh dấu đã reset ({reason})",
                flush=True,
            )
            return True
        writes: list[tuple[tuple[str, ...], int, int | None]] = [
            (_CAP_PATH, cap, _auto_bet_int(ab_dict, "daily_bet_cap_vnd")),
            (_MIN_JP_PATH, min_jp, _auto_bet_int(ab_dict, "min_jackpot_vnd")),
            (_SIDE_LOW_PATH, side_low, _auto_bet_int(ab_dict, "side_total_low_vnd")),
        ]
        changed: list[str] = []
        for path, target, prev in writes:
            if prev is not None and prev == target:
                continue
            if not save_user_config_value(path, target):
                label = ".".join(path)
                print(
                    f"[CAP-RESET] Không ghi được {label}={target:,}",
                    flush=True,
                )
                return False
            prev_s = f"{prev:,}" if prev is not None else "?"
            changed.append(f"{path[-1]} {prev_s} → {target:,}")
        if _target_auto_bet_enabled():
            prev_on = bool(ab_dict.get("enabled")) if isinstance(ab_dict, dict) else False
            if not prev_on:
                if not save_user_config_value(_ENABLED_PATH, True):
                    print("[CAP-RESET] Không ghi được auto_bet.enabled=true", flush=True)
                    return False
                changed.append("enabled false → true")
        _mark_reset_done(
            cap,
            min_jackpot_vnd=min_jp,
            side_total_low_vnd=side_low,
        )
        if changed:
            print(
                f"[CAP-RESET] {', '.join(changed)} ({reason})",
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
            f"giờ VN → cap={_target_cap_vnd():,}, min_jackpot={_target_min_jackpot_vnd():,}, "
            f"side_total_low={_target_side_total_low_vnd():,}"
            + (
                ", auto_bet.enabled=true"
                if _target_auto_bet_enabled()
                else ""
            ),
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
