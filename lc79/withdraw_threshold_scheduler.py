"""
01:00 (Asia/Ho_Chi_Minh) mỗi ngày:
- WITHDRAW_THRESHOLD_MIN → 310000
- ASSIGN_STRATEGY → 3
"""

import threading
import time
from datetime import datetime

import pytz

from constants import load_config, save_config

_TZ = pytz.timezone("Asia/Ho_Chi_Minh")
_TARGET = 310000
_STRATEGY = 3
_CHECK_TIMES = ("01:00", "01:01")
_last_run_date = ""


def _cfg_int(config: dict, key: str, default: int) -> int:
    try:
        return int(config.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _apply_reset() -> bool:
    try:
        config = load_config()
        if not config:
            print("[WITHDRAW-THRESHOLD] ❌ Không đọc được config", flush=True)
            return False

        changed: list[str] = []
        current = _cfg_int(config, "WITHDRAW_THRESHOLD_MIN", _TARGET)
        if current != _TARGET:
            config["WITHDRAW_THRESHOLD_MIN"] = _TARGET
            changed.append(f"WITHDRAW_THRESHOLD_MIN: {current:,} → {_TARGET:,}")

        current_strategy = _cfg_int(config, "ASSIGN_STRATEGY", _STRATEGY)
        if current_strategy != _STRATEGY:
            config["ASSIGN_STRATEGY"] = _STRATEGY
            changed.append(f"ASSIGN_STRATEGY: {current_strategy} → {_STRATEGY}")

        if not changed:
            return True

        if save_config(config):
            print(
                f"[WITHDRAW-THRESHOLD] ✅ 01:00 — {'; '.join(changed)}",
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
            "[WITHDRAW-THRESHOLD] 🕐 01:00 VN — WITHDRAW_THRESHOLD_MIN → 310000, "
            "ASSIGN_STRATEGY → 3",
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
