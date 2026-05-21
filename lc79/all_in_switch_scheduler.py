"""
Scheduler định kỳ 23:30 mỗi ngày chuyển ALL_IN_IF_REMAIN_LT_10K từ 0 sang 1 trong config.json.
"""

import time
from datetime import datetime

from constants import load_config, save_config


def switch_all_in_to_one():
    """
    Chuyển ALL_IN_IF_REMAIN_LT_10K từ 0 sang 1 trong config.json.
    Trả về True nếu thành công, False nếu có lỗi.
    """
    try:
        config = load_config()
        if not config:
            print("[ALL-IN SWITCH] ❌ Không đọc được config", flush=True)
            return False

        config["ALL_IN_IF_REMAIN_LT_10K"] = 1

        if save_config(config):
            print("[ALL-IN SWITCH] ✅ Đã chuyển ALL_IN_IF_REMAIN_LT_10K: 0 → 1 (23:30)", flush=True)
            return True
        else:
            print("[ALL-IN SWITCH] ❌ Không lưu được config", flush=True)
            return False

    except Exception as e:
        print(f"[ALL-IN SWITCH] ❌ Lỗi: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return False


def auto_all_in_switch_scheduler():
    """
    Background task tự động chuyển ALL_IN_IF_REMAIN_LT_10K sang 1 vào 23:30 mỗi ngày.
    Chạy trong thread riêng, check mỗi 60 giây.
    """
    print("[ALL-IN SCHEDULER] 🕐 Đã khởi động scheduler (23:30 mỗi ngày)", flush=True)

    while True:
        try:
            now = datetime.now()
            current_time = now.strftime("%H:%M")
            current_date = now.strftime("%Y-%m-%d")

            if current_time in ["23:30", "23:31"]:
                config = load_config()
                last_run = config.get("LAST_ALL_IN_23_30_RUN", "")

                if last_run != current_date:
                    if config.get("ALL_IN_IF_REMAIN_LT_10K", 0) == 1:
                        # Đã là 1 rồi, chỉ đánh dấu đã chạy
                        config["LAST_ALL_IN_23_30_RUN"] = current_date
                        save_config(config)
                        print("[ALL-IN SCHEDULER] ⏰ 23:30 - ALL_IN_IF_REMAIN_LT_10K đã là 1, bỏ qua", flush=True)
                    else:
                        print("[ALL-IN SCHEDULER] ⏰ 23:30 - Chuyển ALL_IN_IF_REMAIN_LT_10K sang 1...", flush=True)
                        if switch_all_in_to_one():
                            config = load_config()
                            if config:
                                config["LAST_ALL_IN_23_30_RUN"] = current_date
                                save_config(config)

            time.sleep(60)

        except Exception as e:
            print(f"[ALL-IN SCHEDULER] ❌ Lỗi scheduler: {e}", flush=True)
            import traceback
            traceback.print_exc()
            time.sleep(60)
