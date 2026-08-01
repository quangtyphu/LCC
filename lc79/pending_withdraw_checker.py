import time
from collections import defaultdict

import requests

from game_api_helper import NODE_SERVER_URL
from check_withdraw_history import check_withdraw_history, effective_withdraw_status


def _fetch_pending_withdrawals() -> list[dict]:
    """Lấy danh sách lệnh rút đang chờ xử lý từ CMS (kèm transactionId)."""
    try:
        resp = requests.get(
            f"{NODE_SERVER_URL}/api/withdrawals/pending",
            timeout=10,
        )
        if resp.status_code == 404:
            return _fetch_pending_withdrawals_legacy()
        if resp.status_code != 200:
            print(
                f"❌ [PENDING-WD] Lỗi API pending: {resp.status_code}",
                flush=True,
            )
            return []
        data = resp.json()
        if not isinstance(data, list):
            return []
        return [row for row in data if isinstance(row, dict) and row.get("username")]
    except Exception as e:
        print(f"❌ [PENDING-WD] Lỗi gọi API pending: {e}", flush=True)
    return []


def _fetch_pending_withdrawals_legacy() -> list[dict]:
    """Fallback khi CMS chưa có /api/withdrawals/pending."""
    try:
        resp = requests.get(
            f"{NODE_SERVER_URL}/api/withdrawals/pending-users",
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        if not isinstance(data, list):
            return []
        return [{"username": u} for u in data if u]
    except Exception:
        return []


def _sync_pending_for_user(username: str, pending_rows: list[dict]) -> None:
    tx_rows = [r for r in pending_rows if r.get("transactionId")]
    summary = ", ".join(
        f"{r.get('transactionId')} ({int(float(r.get('amount') or 0)):,}đ, {r.get('status')})"
        for r in tx_rows
    ) or f"{len(pending_rows)} lệnh"
    print(
        f"🔍 [PENDING-WD][{username}] Check lịch sử rút — {summary}",
        flush=True,
    )

    result = check_withdraw_history(
        username,
        limit=30,
        return_details=True,
        update_all_if_changed=True,
    )
    if not result.get("ok"):
        print(
            f"❌ [PENDING-WD][{username}] {result.get('error', 'Không lấy được lịch sử rút')}",
            flush=True,
        )
        return

    game_by_id = {
        tx.get("id"): tx
        for tx in (result.get("transactions") or [])
        if tx.get("id")
    }

    for row in tx_rows:
        tx_id = row.get("transactionId")
        db_status = row.get("status") or "pending"
        amount = int(float(row.get("amount") or 0))
        game_tx = game_by_id.get(tx_id)

        if not game_tx:
            print(
                f"⚠️ [PENDING-WD][{username}] Không thấy lệnh {tx_id} "
                f"({amount:,}đ, DB={db_status}) trong lịch sử game",
                flush=True,
            )
            continue

        game_status = effective_withdraw_status(game_tx)
        if game_status == db_status:
            print(
                f"⏳ [PENDING-WD][{username}] Lệnh {tx_id} ({amount:,}đ) "
                f"vẫn: {game_status}",
                flush=True,
            )
            continue

        upd = check_withdraw_history(
            username,
            limit=30,
            target_tx_id=tx_id,
            previous_status=db_status,
            update_if_changed=True,
            return_details=True,
        )
        if upd.get("updated"):
            updated_tx = upd.get("updated_tx") or game_tx
            new_status = effective_withdraw_status(updated_tx)
            print(
                f"✅ [PENDING-WD][{username}] Cập nhật lệnh {tx_id}: "
                f"{db_status} → {new_status}",
                flush=True,
            )
        elif game_status != db_status:
            print(
                f"⚠️ [PENDING-WD][{username}] Game(reason)={game_status}, DB={db_status} "
                f"nhưng chưa cập nhật được lệnh {tx_id}",
                flush=True,
            )

    if not tx_rows:
        if result.get("updated"):
            print(
                f"✅ [PENDING-WD][{username}] Đã đồng bộ lịch sử rút từ game",
                flush=True,
            )
        elif result.get("saved_count", 0) > 0:
            print(
                f"✅ [PENDING-WD][{username}] Đã lưu {result['saved_count']} giao dịch rút mới",
                flush=True,
            )


def pending_withdraw_checker_loop(interval_seconds: int = 600):
    print(
        f"⏱️ [PENDING-WD] Bắt đầu check lịch sử rút mỗi {interval_seconds // 60} phút",
        flush=True,
    )
    while True:
        pending_rows = _fetch_pending_withdrawals()
        if not pending_rows:
            time.sleep(interval_seconds)
            continue

        by_user: dict[str, list[dict]] = defaultdict(list)
        for row in pending_rows:
            by_user[row["username"]].append(row)

        print(
            f"🔎 [PENDING-WD] {len(pending_rows)} lệnh rút đang xử lý "
            f"({len(by_user)} user): {', '.join(by_user.keys())}",
            flush=True,
        )

        for username, rows in by_user.items():
            try:
                _sync_pending_for_user(username, rows)
            except Exception as e:
                print(f"⚠️ [PENDING-WD] Lỗi check {username}: {e}", flush=True)

        time.sleep(interval_seconds)


def start_pending_withdraw_checker(interval_seconds: int = 600):
    import threading

    threading.Thread(
        target=pending_withdraw_checker_loop,
        args=(interval_seconds,),
        daemon=True,
    ).start()
