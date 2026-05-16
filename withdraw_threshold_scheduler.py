"""
01:00 (Asia/Ho_Chi_Minh) mỗi ngày: ép WITHDRAW_THRESHOLD_MIN trong config.json → 310000.
"""

import threading
import time
from datetime import datetime

import pytz

from constants import load_config, save_config

_TZ = pytz.timezone("Asia/Ho_Chi_Minh")
_TARGET = 310000
_CHECK_TIMES = ("01:00", "01:01")
_last_run_date = ""


def _apply_reset() -> bool:
    try:
        config = load_config()
        if not config:
            print("[WITHDRAW-THRESHOLD] ❌ Không đọc được config", flush=True)
            return False

        current = int(config.get("WITHDRAW_THRESHOLD_MIN", _TARGET) or _TARGET)
        if current == _TARGET:
            return True

        config["WITHDRAW_THRESHOLD_MIN"] = _TARGET
        if save_config(config):
            print(
                f"[WITHDRAW-THRESHOLD] ✅ 01:00 — WITHDRAW_THRESHOLD_MIN: "
                f"{current:,} → {_TARGET:,}",
                flush=True,
            )
            return True
        print("[WITHDRAW-THRESHOLD] ❌ Không lưu được config", flush=True)
        return False
    except Exception as e:
        print(f"[WITHDRAW-THRESHOLD] ❌ {e}", flush=True)
        return False


def start_withdraw_threshold_reset_scheduler() -> None:
    global _last_run_date

    def _loop():
        global _last_run_date
        print(
            "[WITHDRAW-THRESHOLD] 🕐 01:00 VN — ép WITHDRAW_THRESHOLD_MIN → 310000",
            flush=True,
        )
        while True:
            try:
                now = datetime.now(_TZ)
                if now.strftime("%H:%M") in _CHECK_TIMES:
                    today = now.strftime("%Y-%m-%d")
                    if _last_run_date != today and _apply_reset():
                        _last_run_date = today
                time.sleep(60)
            except Exception as e:
                print(f"[WITHDRAW-THRESHOLD] ❌ {e}", flush=True)
                time.sleep(60)

    threading.Thread(target=_loop, daemon=True, name="withdraw-threshold-01").start()
