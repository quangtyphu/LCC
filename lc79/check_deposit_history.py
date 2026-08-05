

import threading
import time
from datetime import datetime

import requests

from game_api_helper import game_request_with_retry, NODE_SERVER_URL


def normalize_tx_time(raw: str) -> str:
    """Chuẩn hóa dateTime game → YYYY-MM-DD HH:mm:ss (tránh ISO 'T' làm CMS lọc sai)."""
    if not raw:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    s = str(raw).strip()
    if s.endswith("Z"):
        s = s[:-1]
    s = s.replace("T", " ")
    if "." in s:
        s = s.split(".")[0]
    try:
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return s[:19] if len(s) >= 19 else datetime.now().strftime("%Y-%m-%d %H:%M:%S")

DEPOSIT_HISTORY_POLL_INTERVAL_SEC = 30
DEPOSIT_HISTORY_POLL_MAX_ATTEMPTS = 30

_deposit_poll_lock = threading.Lock()
_deposit_poll_active: set[str] = set()
from get_balance import get_balance

_deposit_refresh_done: set[str] = set()


def refresh_after_deposit_confirm(username: str, order_id=None) -> None:
    """
    Sau khi lệnh nạp Thành Công: làm mới số dư (HTTP) và cập nhật Đang Chơi.
    Không mở WS minigame — watcher sẽ mở handle_ws khi thấy status Đang Chơi.

    Claim NV ngày đã có trong save_deposit_transaction_and_sync_order
    (mọi GD nạp mới >= 200k) — không gọi lại ở đây.
    """
    key = f"{(username or '').strip()}:{order_id}"
    if key in _deposit_refresh_done:
        return
    _deposit_refresh_done.add(key)
    try:
        balance_result = get_balance(username, force=True)
        if not balance_result.get("ok"):
            print(f"⚠️ [{username}] Lỗi lấy balance: {balance_result.get('error')}", flush=True)
        else:
            bal = balance_result.get("balance")
            if bal is not None:
                try:
                    print(f"💰 [{username}] Số dư sau nạp: {int(float(bal)):,}đ", flush=True)
                except (TypeError, ValueError):
                    print(f"💰 [{username}] Số dư sau nạp: {bal}", flush=True)
    except Exception as e:
        print(f"⚠️ [{username}] Lỗi khi cập nhật balance: {e}", flush=True)
    try:
        resp_status = requests.put(
            f"{NODE_SERVER_URL}/api/users/{username}",
            json={"status": "Đang Chơi"},
            timeout=5,
        )
        if resp_status.status_code != 200:
            print(
                f"⚠️ [{username}] Lỗi cập nhật trạng thái API: {resp_status.status_code} {resp_status.text}",
                flush=True,
            )
    except Exception as e:
        print(f"⚠️ [{username}] Không kết nối được API khi update status: {e}", flush=True)


def _ndck_in_tx_content(transfer_content: str, tx_content: str) -> bool:
    """NDCK lệnh phải xuất hiện trong nội dung GD game (không phân biệt hoa thường)."""
    tc = (transfer_content or "").strip()
    if not tc:
        return False
    c = str(tx_content or "").strip()
    if not c:
        return False
    tcl, cl = tc.lower(), c.lower()
    return tcl in cl or cl in tcl


def _sync_deposit_order_by_amount(
    username: str,
    tx_amount: int,
    tx_content: str,
    desired_status: str = "Thành Công",
    only_order_id: int | None = None,
    game_tx_confirmed: bool = False,
) -> bool:
    """
    Khớp lệnh nạp theo username + số tiền + NDCK trong nội dung giao dịch.

    game_tx_confirmed=True: đã có GD SUCCESS trên game — cho phép cập nhật Thành Công
    kể cả lệnh đang Thất Bại/Chờ/Đang nạp (trừ Huỷ).
    """
    if not username or not tx_amount:
        return False
    tx_content_norm = str(tx_content or "").strip()
    if not tx_content_norm:
        return False
    try:
        resp = requests.get(
            f"{NODE_SERVER_URL}/api/deposit-orders",
            params={"username": username, "limit": 50},
            timeout=5,
        )
        if resp.status_code != 200:
            return False
        data = resp.json() or {}
        orders = data.get("data") if isinstance(data.get("data"), list) else (
            data if isinstance(data, list) else []
        )
        if not orders:
            return False
        amt = int(tx_amount)
        for order in orders:
            order_id = order.get("id")
            if only_order_id is not None and order_id is not None:
                if int(order_id) != int(only_order_id):
                    continue
            current_status = (order.get("status") or "").strip()
            try:
                order_amount = int(float(order.get("amount") or 0))
            except (TypeError, ValueError):
                order_amount = 0
            if amt != order_amount:
                continue
            order_tc = str(
                order.get("transferContent")
                or order.get("transfer_content")
                or ""
            ).strip()
            if not order_tc:
                continue
            if not _ndck_in_tx_content(order_tc, tx_content_norm):
                continue
            if current_status == desired_status:
                return True
            if current_status == "Huỷ":
                return False
            if not game_tx_confirmed:
                if current_status not in ("Đã Nạp", "Thất Bại"):
                    continue
                if current_status == "Thất Bại" and only_order_id is None:
                    continue
            update_resp = requests.put(
                f"{NODE_SERVER_URL}/api/deposit-orders/{order_id}",
                json={"status": desired_status},
                timeout=5,
            )
            if update_resp.status_code in (200, 204):
                print(f"✅ Đã cập nhật deposit_orders #{order_id} → {desired_status}", flush=True)
                if desired_status == "Thành Công":
                    try:
                        from auto_deposit_on_out_of_money import (
                            clear_order_sent_to_banking,
                            remove_from_deposit_cache,
                        )
                        remove_from_deposit_cache(username)
                        clear_order_sent_to_banking(order_id)
                    except Exception as e:
                        print(f"⚠️ [{username}] Không dọn cache sau sync order #{order_id}: {e}", flush=True)
                return True
            return False
        return False
    except Exception as e:
        print(f"⚠️ Lỗi sync deposit_orders theo số tiền: {e}", flush=True)
        return False


def after_deposit_transaction_saved(
    username: str,
    tx_amount: int | float,
    tx_content: str,
) -> bool:
    """
    Sau khi lưu GD nạp MỚI vào transaction-details (bất kể nguồn nào):
    khớp username + số tiền + NDCK → cập nhật deposit_orders «Thành Công»
    nếu trạng thái hiện tại khác «Thành Công».
    """
    u = (username or "").strip()
    if not u:
        return False
    try:
        amt = int(float(tx_amount))
    except (TypeError, ValueError):
        return False
    content = str(tx_content or "").strip()
    if not amt or not content:
        return False
    return _sync_deposit_order_by_amount(
        u,
        amt,
        content,
        desired_status="Thành Công",
        game_tx_confirmed=True,
    )


def _fetch_deposit_order_device_nap(order_id=None, username: str = "") -> str:
    """Lấy deviceNap đã lưu trên deposit_orders (CMS)."""
    if order_id is None:
        return ""
    try:
        r = requests.get(f"{NODE_SERVER_URL}/api/deposit-orders/{order_id}", timeout=5)
        if r.ok:
            row = r.json() or {}
            return str(row.get("deviceNap") or row.get("device_nap") or "").strip()
    except Exception:
        pass
    if not username:
        return ""
    try:
        r = requests.get(
            f"{NODE_SERVER_URL}/api/deposit-orders",
            params={"username": username, "limit": 50},
            timeout=5,
        )
        if not r.ok:
            return ""
        data = r.json() or {}
        orders = data.get("data") if isinstance(data.get("data"), list) else (
            data if isinstance(data, list) else []
        )
        for order in orders:
            if str(order.get("id")) == str(order_id):
                return str(order.get("deviceNap") or order.get("device_nap") or "").strip()
    except Exception:
        pass
    return ""


def save_deposit_transaction_and_sync_order(
    username: str,
    *,
    transaction_id,
    amount: int | float,
    content: str,
    time: str = "",
    nickname: str = "",
    device_nap: str = "",
    order_id=None,
) -> tuple[bool, bool]:
    """
    Lưu GD nạp vào transaction-details (bỏ qua 409) rồi sync deposit_orders.
    Dùng chung cho check_deposit_history, fetch_transactions (WS reconnect), v.v.
    Trả (saved_new, synced_order_success).
    """
    u = (username or "").strip()
    if not u:
        return False, False
    dev_nap = (device_nap or "").strip() or _fetch_deposit_order_device_nap(order_id, u)
    record = {
        "username": u,
        "nickname": nickname or u,
        "hinhThuc": "Nạp tiền",
        "transactionId": transaction_id,
        "amount": float(amount or 0),
        "time": normalize_tx_time(time),
        "deviceNap": dev_nap,
    }
    saved_new = False
    try:
        resp = requests.post(
            f"{NODE_SERVER_URL}/api/transaction-details",
            json=record,
            timeout=5,
        )
        if resp.status_code in (200, 201):
            saved_new = True
            print(
                f"Đã lưu 1 giao dịch nạp {int(float(amount)):,} cho [{u}] "
                f"với nội dung {content}",
                flush=True,
            )
        elif resp.status_code != 409:
            print(
                f"⚠️ [{u}] Lỗi lưu giao dịch {transaction_id}: "
                f"{resp.status_code} - {resp.text}",
                flush=True,
            )
    except Exception as e:
        print(f"⚠️ [{u}] Lỗi lưu giao dịch {transaction_id}: {e}", flush=True)

    synced = False
    if saved_new:
        synced = after_deposit_transaction_saved(u, amount, content)
        if float(amount or 0) >= 200000:
            _schedule_claim_missions_after_deposit(u)
    return saved_new, synced


def _schedule_claim_missions_after_deposit(username: str) -> None:
    """
    Sau GD nạp mới >= 200k: nhận NV ngày (isWon, chưa claim).
    Chạy nền + retry ngắn — mission game đôi khi cập nhật chậm hơn lịch sử nạp.
    """
    u = (username or "").strip()
    if not u:
        return

    def _run() -> None:
        try:
            from mission_api import auto_claim_missions

            for attempt in range(1, 4):
                claimed = auto_claim_missions(u)
                if claimed:
                    return
                if attempt < 3:
                    time.sleep(5)
        except Exception as e:
            print(f"⚠️ [{u}] Lỗi gọi auto_claim_missions: {e}", flush=True)

    threading.Thread(target=_run, daemon=True, name=f"claim-mission-{u}").start()


def _try_sync_pending_order_from_transactions(
    username: str,
    transactions: list,
    order_id=None,
    amount=None,
    transfer_content: str | None = None,
) -> bool:
    """
    GD có thể đã lưu trước (409 / fetch_transactions khi WS reconnect) —
    vẫn khớp lệnh đang «Đã Nạp» với lịch sử game để poll dừng đúng.
    """
    if order_id is None:
        return False
    try:
        oid = int(order_id)
        poll_amt = int(float(amount or 0))
    except (TypeError, ValueError):
        return False
    poll_tc = (transfer_content or "").strip()
    for tx in transactions or []:
        try:
            tx_amt = int(float(tx.get("amount") or 0))
        except (TypeError, ValueError):
            continue
        if poll_amt and tx_amt != poll_amt:
            continue
        tx_content = str(tx.get("content") or "")
        if poll_tc and not _ndck_in_tx_content(poll_tc, tx_content):
            continue
        if _sync_deposit_order_by_amount(
            username,
            tx_amt,
            tx_content,
            only_order_id=oid,
            game_tx_confirmed=True,
        ):
            return True
    return False


def check_deposit_history(
    username,
    transfer_content=None,
    order_id=None,
    amount=None,
    limit=10,
    status=None,
):

    """
    Lấy lịch sử nạp tiền từ game, lưu giao dịch mới vào DB, tự động nhận quà nếu đủ điều kiện.
    Sử dụng game_api_helper để lấy token, proxy, headers, params.
    """
    api_url = "https://wsslot.tele68.com/v1/lobby/transaction/history"
    params = {
        "limit": limit,
        "channel_id": 2,
        "type": "DEPOSIT",
        "status": "SUCCESS"
    }
    resp = game_request_with_retry(username, "GET", api_url, params=params)
    if not resp or resp.status_code != 200:
        print(f"❌ [{username}] Lỗi lấy lịch sử: {resp.status_code if resp else 'No response'}", flush=True)
        return {"ok": False, "error": f"Lỗi lấy lịch sử: {resp.status_code if resp else 'No response'}"}

    try:
        transactions_raw = resp.json()
        transactions = []
        for tx in transactions_raw:
            transactions.append({
                "id": tx.get("id"),
                "amount": int(tx.get("amount", 0)),
                "content": tx.get("content"),
                "status": tx.get("status"),
                "dateTime": tx.get("dateTime"),
                "reason": tx.get("reason")
            })
        total = len(transactions)
    except Exception as e:
        print(f"❌ [{username}] Lỗi parse lịch sử: {e}", flush=True)
        return {"ok": False, "error": str(e)}

    # 2. Lưu giao dịch mới vào DB (bỏ qua 409 nếu GD đã có).
    saved = []
    new_saved = 0
    synced_order_success = False
    order_device_nap = _fetch_deposit_order_device_nap(order_id, username)
    for tx in transactions:
        saved_new_this_tx, synced_this_tx = save_deposit_transaction_and_sync_order(
            username,
            transaction_id=tx.get("id"),
            amount=tx.get("amount", 0),
            content=str(tx.get("content") or ""),
            time=tx.get("dateTime"),
            nickname=username,
            device_nap=order_device_nap,
            order_id=order_id,
        )
        if saved_new_this_tx:
            saved.append({
                "username": username,
                "transactionId": tx.get("id"),
                "amount": float(tx.get("amount", 0)),
                "time": tx.get("dateTime"),
            })
            new_saved += 1
        if synced_this_tx:
            synced_order_success = True

    if not synced_order_success:
        synced_order_success = _try_sync_pending_order_from_transactions(
            username,
            transactions,
            order_id=order_id,
            amount=amount,
            transfer_content=transfer_content,
        )

    if synced_order_success:
        order_status = _deposit_order_status(order_id, username, transfer_content)
        if order_status == "Thành Công":
            refresh_after_deposit_confirm(username, order_id=order_id)

    return {
        "ok": True,
        "total": total,
        "transactions": transactions,
        "new_saved": new_saved,
        "synced_order_success": synced_order_success,
    }


def _deposit_order_status(
    order_id,
    username: str = "",
    transfer_content: str = "",
) -> str:
    """Đọc trạng thái lệnh nạp từ Node (rỗng nếu lỗi)."""
    if order_id is None:
        return ""
    try:
        from deposit_callback_routing import get_lc79_order

        row = get_lc79_order(order_id, transfer_content)
        if row:
            return str(row.get("status") or row.get("Status") or "").strip()
    except Exception:
        pass
    u = (username or "").strip()
    if u:
        try:
            resp = requests.get(
                f"{NODE_SERVER_URL}/api/deposit-orders",
                params={"username": u, "limit": 50},
                timeout=5,
            )
            if resp.ok:
                data = resp.json() or {}
                orders = data.get("data") if isinstance(data.get("data"), list) else (
                    data if isinstance(data, list) else []
                )
                for order in orders:
                    if str(order.get("id")) == str(order_id):
                        return str(
                            order.get("status") or order.get("Status") or ""
                        ).strip()
        except Exception:
            pass
    try:
        resp = requests.get(
            f"{NODE_SERVER_URL}/api/deposit-orders/{order_id}",
            timeout=5,
        )
        if not resp.ok:
            return ""
        js = resp.json() or {}
        order = js.get("data")
        if isinstance(order, list) and order:
            order = order[0]
        if not isinstance(order, dict):
            order = js.get("order") or js.get("depositOrder") or js
        if not isinstance(order, dict):
            return ""
        return str(order.get("status") or order.get("Status") or "").strip()
    except Exception:
        return ""


def _try_begin_deposit_poll(key: str) -> bool:
    with _deposit_poll_lock:
        if key in _deposit_poll_active:
            return False
        _deposit_poll_active.add(key)
        return True


def _end_deposit_poll(key: str) -> None:
    with _deposit_poll_lock:
        _deposit_poll_active.discard(key)


def _log_deposit_poll_success(username: str, order_id, amount=None) -> None:
    u = (username or "").strip()
    extra = ""
    if amount:
        try:
            extra = f" ({int(amount):,}đ)"
        except (TypeError, ValueError):
            pass
    oid = f" #{order_id}" if order_id is not None else ""
    print(f"✅ [{u}] Xác nhận nạp thành công order{oid}{extra}", flush=True)


def _handle_deposit_poll_failed(
    username: str,
    order_id,
    amount,
    transfer_content: str,
    attempts: int,
    interval_sec: int,
) -> None:
    from deposit_api import update_deposit_order_status

    u = (username or "").strip()
    if not u or order_id is None:
        print(f"❌ [{u or '?'}] Không xác nhận nạp sau {attempts} lần check", flush=True)
        return

    try:
        amt = int(amount or 0)
    except (TypeError, ValueError):
        amt = 0
    tc = (transfer_content or "").strip()

    if _deposit_order_status(order_id, u, tc) == "Thành Công":
        _log_deposit_poll_success(u, order_id, amt)
        return

    if not update_deposit_order_status(order_id, "Thất Bại"):
        print(
            f"❌ [{u}] Không xác nhận nạp sau {attempts} lần — không cập nhật được order #{order_id}",
            flush=True,
        )
        return

    try:
        from auto_deposit_on_out_of_money import remove_from_deposit_cache

        remove_from_deposit_cache(u)
    except Exception:
        pass

    print(
        f"❌ [{u}] Không xác nhận nạp sau {attempts} lần — order #{order_id} → Thất Bại",
        flush=True,
    )

    try:
        from datetime import datetime

        from telegram_notifier import send_telegram

        send_telegram(
            f"❌ LỆNH NẠP TIỀN THẤT BẠI\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 Username: {u}\n"
            f"🆔 Order ID: #{order_id}\n"
            f"💰 Số tiền: {amt:,}đ\n"
            f"📝 NDCK: {tc}\n"
            f"⏰ Thời gian: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Không tìm thấy giao dịch sau {attempts} lần check ({interval_sec}s/lần)."
        )
    except Exception as e:
        print(f"⚠️ [{u}] Lỗi gửi Telegram: {e}", flush=True)


def poll_deposit_history_after_da_nap(
    username: str,
    order_id=None,
    transfer_content=None,
    amount=None,
    interval_sec: int = DEPOSIT_HISTORY_POLL_INTERVAL_SEC,
    max_attempts: int = DEPOSIT_HISTORY_POLL_MAX_ATTEMPTS,
    limit: int = 20,
) -> dict:
    """
    Sau callback «Đã Nạp» từ bên thứ 3: check lịch sử nạp game định kỳ.
    Mặc định mỗi 30s, tối đa 30 lần; dừng sớm khi lệnh → Thành Công.
    """
    u = (username or "").strip()
    if not u:
        return {"ok": False, "error": "missing username"}

    poll_key = str(order_id) if order_id is not None else u
    if not _try_begin_deposit_poll(poll_key):
        return {"ok": True, "skipped": "poll_in_progress"}

    tc = (transfer_content or "").strip()
    try:
        if _deposit_order_status(order_id, u, tc) == "Thành Công":
            return {"ok": True, "confirmed": True, "skipped": "already_thanh_cong"}

        interval = max(1, int(interval_sec))
        attempts = max(1, int(max_attempts))

        for attempt in range(1, attempts + 1):
            if _deposit_order_status(order_id, u, tc) == "Thành Công":
                _log_deposit_poll_success(u, order_id, amount)
                return {"ok": True, "confirmed": True, "attempt": attempt}

            result = check_deposit_history(
                u,
                transfer_content=transfer_content,
                order_id=order_id,
                amount=amount,
                limit=limit,
            )

            if result.get("synced_order_success") or _deposit_order_status(order_id, u, tc) == "Thành Công":
                _log_deposit_poll_success(u, order_id, amount)
                return {"ok": True, "confirmed": True, "attempt": attempt}

            if attempt < attempts:
                time.sleep(interval)

        _handle_deposit_poll_failed(u, order_id, amount, transfer_content or "", attempts, interval)
        return {"ok": True, "confirmed": False, "attempts": attempts}
    finally:
        _end_deposit_poll(poll_key)


# Cho phép chạy trực tiếp file này
if __name__ == "__main__":
    username = input("Nhập username: ").strip()
    if not username:
        print(f"❌ Username không được để trống [{username}]")
        exit(1)
    result = check_deposit_history(username)
    print(f"\nKết quả cho [{username}]:")
    print(f"[{username}] {result}")
