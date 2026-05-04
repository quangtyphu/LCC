
import asyncio
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from game_api_helper import game_request_with_retry, NODE_SERVER_URL
from get_balance import get_balance
from ws_minigame_client import connect_minigame

_TZ_VN = ZoneInfo("Asia/Ho_Chi_Minh")


def _extract_order_from_api(js: dict) -> dict:
    if not isinstance(js, dict):
        return {}
    d = js.get("data")
    if isinstance(d, dict):
        return d
    if isinstance(d, list) and d and isinstance(d[0], dict):
        return d[0]
    return js


def _parse_tx_datetime(val) -> datetime | None:
    """Parse dateTime từ API game / createdAt từ DB → timezone VN."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        try:
            return datetime.fromtimestamp(val, tz=_TZ_VN)
        except (OSError, ValueError, OverflowError):
            return None
    s = str(val).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_TZ_VN)
        return dt.astimezone(_TZ_VN)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt).replace(tzinfo=_TZ_VN)
        except Exception:
            continue
    return None


def get_deposit_order_meta(order_id: int) -> tuple[datetime | None, str]:
    """
    Đọc lệnh nạp từ API: (mốc thời gian sớm nhất để khớp GD game, NDCK).
    Mốc = createdAt - 5 phút (trừ lệch giờ). NDCK dùng khi không có mốc.
    """
    if not order_id:
        return None, ""
    try:
        r = requests.get(f"{NODE_SERVER_URL}/api/deposit-orders/{order_id}", timeout=5)
        if r.status_code != 200:
            return None, ""
        order = _extract_order_from_api(r.json() or {})
        raw_created = order.get("createdAt") or order.get("created_at")
        dt = _parse_tx_datetime(raw_created)
        sync_min = (dt - timedelta(minutes=5)) if dt is not None else None
        tc = (
            order.get("transferContent")
            or order.get("transfer_content")
            or ""
        )
        return sync_min, str(tc).strip()
    except Exception:
        return None, ""


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


def tx_matches_deposit_order(
    tx: dict,
    expected_amount: int,
    sync_min: datetime | None,
    transfer_content: str,
) -> bool:
    """
    Tránh báo Thành Công nhầm: không chỉ trùng số tiền / mốc thời gian với GD cũ.
    - Có NDCK: bắt buộc nội dung GD chứa đúng NDCK (cùng với điều kiện thời gian nếu có sync_min).
    - Không có NDCK nhưng có sync_min: chỉ khớp theo thời gian + số tiền (trường hợp hiếm API không trả NDCK).
    - Không NDCK và không sync_min: không khớp.
    """
    try:
        amt = int(tx.get("amount") or 0)
    except (TypeError, ValueError):
        return False
    if amt != int(expected_amount):
        return False
    tx_dt = _parse_tx_datetime(tx.get("dateTime"))
    tc = (transfer_content or "").strip()
    content = str(tx.get("content") or "")

    if tc and not _ndck_in_tx_content(tc, content):
        return False

    if sync_min is not None:
        if tx_dt is None:
            return False
        return tx_dt >= sync_min

    return bool(tc)


def refresh_after_deposit_confirm(username: str) -> None:
    """
    Sau khi chắc chắn tiền đã vào (lệnh Thành Công / giao dịch mới lưu):
    làm mới số dư (bỏ cooldown), cập nhật trạng thái user, WS minigame.
    """
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

    try:
        coro = connect_minigame(username, keep_alive=False)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
        else:
            loop.create_task(coro)
    except Exception as e:
        print(f"⚠️ [{username}] Lỗi gọi WS minigame sau nạp: {e}", flush=True)


def _sync_deposit_order_by_amount(
    username: str,
    tx_amount: int,
    desired_status: str = "Thành Công",
    only_order_id: int | None = None,
) -> bool:
    """
    Khớp lệnh nạp theo username + số tiền.
    Chỉ nâng lên Thành Công khi lệnh đã «Đã Nạp» (callback bên thứ 3), không sync khi còn Chờ/Đang nạp.
    only_order_id: nếu có, chỉ cập nhật đúng lệnh đó (tránh trùng số tiền nhiều lệnh).
    """
    if not username or not tx_amount:
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
            if current_status == desired_status:
                return True
            if current_status == "Huỷ":
                return False
            # Không ghi Thành Công nếu chưa qua Đã Nạp (tránh báo thành công ngay khi vừa tạo lệnh)
            if current_status != "Đã Nạp":
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
                        from auto_deposit_on_out_of_money import remove_from_deposit_cache
                        remove_from_deposit_cache(username)
                    except Exception as e:
                        print(f"⚠️ [{username}] Không xóa được khỏi cache sau sync theo số tiền: {e}", flush=True)
                return True
            return False
        return False
    except Exception as e:
        print(f"⚠️ Lỗi sync deposit_orders theo số tiền: {e}", flush=True)
        return False

def check_deposit_history(
    username,
    transfer_content=None,
    order_id=None,
    amount=None,
    limit=10,
    status=None,
    sync_min_datetime=None,
    sync_only_order_id=None,
    sync_transfer_content=None,
):

    """
    Lấy lịch sử nạp tiền từ game, lưu giao dịch mới vào DB, tự động nhận quà nếu đủ điều kiện.
    Sử dụng game_api_helper để lấy token, proxy, headers, params.

    sync_min_datetime / sync_only_order_id / sync_transfer_content:
      Khi theo dõi một lệnh cụ thể (wait_and_check), bắt buộc lọc GD theo thời gian hoặc NDCK
      trước khi sync Thành Công — tránh khớp nhầm GD cũ cùng số tiền.
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

    # 2. Lưu giao dịch mới vào DB thực tế
    # Thứ tự bình thường (giao dịch mới): (1) dòng "Đã lưu 1 giao dịch nạp ..." rồi (2) "✅ Đã cập nhật deposit_orders #... → Thành Công"
    saved = []
    new_saved = 0
    synced_order_success = False
    sync_strict = (
        sync_min_datetime is not None
        or (sync_transfer_content or "").strip()
        or sync_only_order_id is not None
    )
    for tx in transactions:
        record = {
            "username": username,
            "nickname": username,  # Nếu có nickname thực thì truyền vào
            "hinhThuc": "Nạp tiền",
            "transactionId": tx.get("id"),
            "amount": float(tx.get("amount", 0)),
            "time": tx.get("dateTime"),
            "deviceNap": "",
        }
        try:
            resp2 = requests.post(f"{NODE_SERVER_URL}/api/transaction-details", json=record, timeout=5)
            if resp2.status_code in (200, 201):
                saved.append(record)
                new_saved += 1
                print(f"Đã lưu 1 giao dịch nạp {int(tx['amount']):,} cho [{username}] với nội dung {tx['content']}", flush=True)
                try:
                    resp_json = resp2.json()
                    is_first = resp_json.get("isFirstDepositToday")
                    is_bonus = resp_json.get("isEligibleForBonus")
                    if (is_first or is_bonus) and float(tx["amount"]) >= 200000:
                        # Gọi nhận nhiệm vụ tự động
                        try:
                            from mission_api import auto_claim_missions
                            auto_claim_missions(username)
                        except Exception as e:
                            print(f"⚠️ [{username}] Lỗi gọi auto_claim_missions: {e}", flush=True)
                except Exception:
                    pass
            elif resp2.status_code != 409:
                print(f"⚠️ [{username}] Lỗi lưu giao dịch {tx.get('id')} cho [{username}]: {resp2.status_code} - {resp2.text}", flush=True)
        except Exception as e:
            print(f"⚠️ [{username}] Lỗi lưu giao dịch {tx.get('id')} cho [{username}]: {e}", flush=True)

        # Sync deposit_orders theo số tiền; có chế độ strict khi theo dõi lệnh (tránh GD cũ cùng amount)
        tx_amount = tx.get("amount", 0)
        if tx_amount:
            if sync_strict:
                if not tx_matches_deposit_order(
                    tx,
                    int(tx_amount),
                    sync_min_datetime,
                    (sync_transfer_content or "").strip(),
                ):
                    continue
            if _sync_deposit_order_by_amount(
                username,
                int(tx_amount),
                desired_status="Thành Công",
                only_order_id=sync_only_order_id,
            ):
                synced_order_success = True

    # Có giao dịch mới lưu DB HOẶC vừa đồng bộ lệnh → Đã Nạp → Thành Công: đều cần làm mới số dư
    # (Trước đây chỉ khi new_saved > 0 nên khi giao dịch đã tồn tại 409 thì không gọi get_balance.)
    if new_saved > 0 or synced_order_success:
        refresh_after_deposit_confirm(username)

    return {"ok": True, "total": total, "transactions": transactions}


# Cho phép chạy trực tiếp file này
if __name__ == "__main__":
    username = input("Nhập username: ").strip()
    if not username:
        print(f"❌ Username không được để trống [{username}]")
        exit(1)
    result = check_deposit_history(username)
    print(f"\nKết quả cho [{username}]:")
    print(f"[{username}] {result}")
