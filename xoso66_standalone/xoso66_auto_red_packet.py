# -*- coding: utf-8 -*-
"""
Tự động nhận lì xì — 21:00 giờ VN, 1 lần/ngày cho toàn bộ acc.

Gọi xoso66_red_packet.run_batch("all"). Mỗi acc vẫn tuân once_per_vn_day trong
xoso66_red_packet (bỏ qua nếu đã nhận thành công hôm nay).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from xoso66_config_util import load_config
from xoso66_paths import cms_game_data_dir
from xoso66_shutdown import stopping
from xoso66_time_util import now_vn, today_vn_str

_RUN_LOCK = threading.Lock()
_STATE_FILE = Path(cms_game_data_dir()) / "red_packet_daily_run_state.json"


def _cfg() -> dict[str, Any]:
    raw = load_config().get("auto_red_packet")
    return raw if isinstance(raw, dict) else {}


def auto_red_packet_enabled() -> bool:
    return bool(_cfg().get("enabled", False))


def _schedule_hour() -> int:
    return int(_cfg().get("hour", 21))


def _schedule_minute() -> int:
    return int(_cfg().get("minute", 0))


def _parallel() -> int:
    return max(1, int(_cfg().get("parallel", 5)))


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


def batch_ran_today_vn() -> bool:
    """Đã chạy batch all hôm nay (giờ VN) — tránh gọi lại khi restart main."""
    day = today_vn_str()
    st = _load_state()
    return str(st.get("vn_day") or "") == day and bool(st.get("batch_ran"))


def _mark_batch_started() -> None:
    from datetime import datetime, timezone

    _save_state(
        {
            "vn_day": today_vn_str(),
            "batch_ran": True,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
    )


def _due_for_daily_run() -> bool:
    if batch_ran_today_vn():
        return False
    now = now_vn()
    target = now.replace(
        hour=_schedule_hour(),
        minute=_schedule_minute(),
        second=0,
        microsecond=0,
    )
    return now >= target


def run_daily_red_packet_batch(*, reason: str = "scheduled") -> dict[str, Any] | None:
    """Chạy all — trả None nếu đã chạy hôm nay hoặc đang busy."""
    if not _RUN_LOCK.acquire(blocking=False):
        print("[AUTO-LIXI] Đang chạy batch khác — bỏ qua", flush=True)
        return None
    try:
        if batch_ran_today_vn():
            return None
        _mark_batch_started()
        from xoso66_red_packet import run_batch

        print(
            f"[AUTO-LIXI] Bắt đầu batch all ({reason}) — "
            f"song song {_parallel()} nick",
            flush=True,
        )
        rep = run_batch("all", parallel=_parallel(), headless=True)
        claimed = int(rep.get("claimed_count") or 0)
        total = int(rep.get("total") or 0)
        amt = int(rep.get("total_amount") or 0)
        print(
            f"[AUTO-LIXI] Xong batch — nhận {claimed}/{total} nick, "
            f"tổng +{amt:,} VND ({rep.get('elapsed_sec')}s)",
            flush=True,
        )
        return rep
    except Exception as e:
        print(f"[AUTO-LIXI] Lỗi batch: {e}", flush=True)
        return {"ok": False, "error": str(e)}
    finally:
        _RUN_LOCK.release()


def worker_auto_red_packet_loop(*, quiet: bool = False) -> None:
    if not auto_red_packet_enabled():
        return
    tick = _worker_tick_sec()
    if not quiet:
        print(
            f"[AUTO-LIXI] Worker: {_schedule_hour():02d}:{_schedule_minute():02d} "
            f"giờ VN, 1 lần/ngày, parallel {_parallel()}",
            flush=True,
        )
    while not stopping():
        try:
            if _due_for_daily_run():
                run_daily_red_packet_batch(reason="21h VN")
        except Exception as e:
            if not stopping():
                print(f"[AUTO-LIXI] Lỗi vòng quét: {e}", flush=True)
        for _ in range(max(1, int(tick))):
            if stopping():
                break
            time.sleep(1)
    print("[AUTO-LIXI] Worker đã dừng.", flush=True)


def start_auto_red_packet_thread(*, quiet: bool = False) -> threading.Thread | None:
    if not auto_red_packet_enabled():
        return None
    t = threading.Thread(
        target=worker_auto_red_packet_loop,
        kwargs={"quiet": quiet},
        daemon=False,
        name="xoso66-auto-lixi",
    )
    t.start()
    return t
