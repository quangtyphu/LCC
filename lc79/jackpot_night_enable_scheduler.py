"""
02:00 (Asia/Ho_Chi_Minh) mỗi ngày:
- JACKPOT_NIGHT_EXTEND.ENABLED → 1
"""

import threading
import time
from datetime import datetime

import pytz

from constants import load_config, save_config

_TZ = pytz.timezone("Asia/Ho_Chi_Minh")
_CHECK_TIMES = ("02:00", "02:01")
_last_run_date = ""


def _apply_night_enable() -> bool:
    try:
        config = load_config()
        if not config:
            print("[NIGHT-ENABLE] ❌ Không đọc được config", flush=True)
            return False

        jackpot = config.get("JACKPOT_NIGHT_EXTEND")
        if not isinstance(jackpot, dict):
            jackpot = {}
            config["JACKPOT_NIGHT_EXTEND"] = jackpot
        current_je = int(jackpot.get("ENABLED", 0) or 0)
        if current_je == 1:
            return True

        jackpot["ENABLED"] = 1
        if save_config(config):
            print(f"[NIGHT-ENABLE] ✅ 02:00 — JACKPOT_NIGHT_EXTEND.ENABLED: {current_je} → 1", flush=True)
            return True
        print("[NIGHT-ENABLE] ❌ Không lưu được config", flush=True)
        return False
    except Exception as e:
        print(f"[NIGHT-ENABLE] ❌ {e}", flush=True)
        return False


def start_jackpot_night_enable_scheduler() -> None:
    global _last_run_date

    def _loop():
        global _last_run_date
        print(
            "[NIGHT-ENABLE] 🕐 02:00 VN — JACKPOT_NIGHT_EXTEND.ENABLED → 1",
            flush=True,
        )
        while True:
            try:
                now = datetime.now(_TZ)
                if now.strftime("%H:%M") in _CHECK_TIMES:
                    today = now.strftime("%Y-%m-%d")
                    if _last_run_date != today and _apply_night_enable():
                        _last_run_date = today
                time.sleep(60)
            except Exception as e:
                print(f"[NIGHT-ENABLE] ❌ {e}", flush=True)
                time.sleep(60)

    threading.Thread(target=_loop, daemon=True, name="night-enable-02").start()
