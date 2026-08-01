"""
09:00 (Asia/Ho_Chi_Minh) mỗi ngày:
- JACKPOT_NIGHT_EXTEND.ENABLED → 0
- WITHDRAW_THRESHOLD_MIN → 510000
"""

import threading
import time
from datetime import datetime

import pytz

from constants import load_config, save_config

_TZ = pytz.timezone("Asia/Ho_Chi_Minh")
_CHECK_TIMES = ("09:00", "09:01")
_WITHDRAW_TARGET = 510000
_last_run_date = ""


def _apply_morning_reset() -> bool:
    try:
        config = load_config()
        if not config:
            print("[MORNING-RESET] ❌ Không đọc được config", flush=True)
            return False

        changed: list[str] = []

        jackpot = config.get("JACKPOT_NIGHT_EXTEND")
        if not isinstance(jackpot, dict):
            jackpot = {}
            config["JACKPOT_NIGHT_EXTEND"] = jackpot
        current_je = int(jackpot.get("ENABLED", 0) or 0)
        if current_je != 0:
            jackpot["ENABLED"] = 0
            changed.append(f"JACKPOT_NIGHT_EXTEND.ENABLED: {current_je} → 0")

        current_wt = int(config.get("WITHDRAW_THRESHOLD_MIN", _WITHDRAW_TARGET) or _WITHDRAW_TARGET)
        if current_wt != _WITHDRAW_TARGET:
            config["WITHDRAW_THRESHOLD_MIN"] = _WITHDRAW_TARGET
            changed.append(f"WITHDRAW_THRESHOLD_MIN: {current_wt:,} → {_WITHDRAW_TARGET:,}")

        if not changed:
            return True

        if save_config(config):
            print(f"[MORNING-RESET] ✅ 09:00 — {'; '.join(changed)}", flush=True)
            return True
        print("[MORNING-RESET] ❌ Không lưu được config", flush=True)
        return False
    except Exception as e:
        print(f"[MORNING-RESET] ❌ {e}", flush=True)
        return False


def start_jackpot_morning_reset_scheduler() -> None:
    global _last_run_date

    def _loop():
        global _last_run_date
        print(
            "[MORNING-RESET] 🕐 09:00 VN — JACKPOT_NIGHT_EXTEND.ENABLED → 0, "
            f"WITHDRAW_THRESHOLD_MIN → {_WITHDRAW_TARGET:,}",
            flush=True,
        )
        while True:
            try:
                now = datetime.now(_TZ)
                if now.strftime("%H:%M") in _CHECK_TIMES:
                    today = now.strftime("%Y-%m-%d")
                    if _last_run_date != today and _apply_morning_reset():
                        _last_run_date = today
                time.sleep(60)
            except Exception as e:
                print(f"[MORNING-RESET] ❌ {e}", flush=True)
                time.sleep(60)

    threading.Thread(target=_loop, daemon=True, name="morning-reset-09").start()
