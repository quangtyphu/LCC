import time
import requests

from game_api_helper import NODE_SERVER_URL
from check_withdraw_history import check_withdraw_history


def _fetch_pending_users() -> list:
    try:
        resp = requests.get(
            f"{NODE_SERVER_URL}/api/withdrawals/pending-users",
            timeout=10
        )
        if resp.status_code != 200:
            print(
                f"❌ [PENDING-WD] Lỗi API pending-users: {resp.status_code}",
                flush=True
            )
            return []
        data = resp.json()
        if isinstance(data, list):
            return [u for u in data if u]
    except Exception as e:
        print(f"❌ [PENDING-WD] Lỗi gọi API pending-users: {e}", flush=True)
    return []


def pending_withdraw_checker_loop(interval_seconds: int = 600):
    print(
        f"⏱️ [PENDING-WD] Bắt đầu check lịch sử rút mỗi {interval_seconds // 60} phút",
        flush=True
    )
    while True:
        users = _fetch_pending_users()
        if users:
            print(f"🔎 [PENDING-WD] {len(users)} user đang chờ rút", flush=True)
        for username in users:
            try:
                check_withdraw_history(username, limit=10)
            except Exception as e:
                print(f"⚠️ [PENDING-WD] Lỗi check {username}: {e}", flush=True)
        time.sleep(interval_seconds)


def start_pending_withdraw_checker(interval_seconds: int = 600):
    import threading
    threading.Thread(
        target=pending_withdraw_checker_loop,
        args=(interval_seconds,),
        daemon=True
    ).start()
