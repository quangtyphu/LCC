# -*- coding: utf-8 -*-
"""
Nạp qua bên thứ 3 (giống LC79).

  1. Tạo đơn XOSO66 + QR
  2. POST bên thứ 3
  3. Callback «Đã Nạp» → poll: 10 lệnh nạp gần nhất, Hoàn tất mới lưu DB = OK

Chạy: python xoso66_third_party_deposit_handler.py
"""

from __future__ import annotations

import base64
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, request

from xoso66_config_util import configure_stdio_utf8

configure_stdio_utf8()

logging.getLogger("werkzeug").setLevel(logging.ERROR)

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

from deposit_callback_routing import resolve_callback_game


def _load_urls() -> tuple[str, str, int]:
    from xoso66_config_util import load_config

    cfg = load_config()
    ad = cfg.get("auto_deposit") if isinstance(cfg.get("auto_deposit"), dict) else {}
    third = str(
        ad.get("third_party_url")
        or os.environ.get("XOSO66_THIRD_PARTY_DEPOSIT_URL", "http://localhost:8888/api/deposit")
    ).strip()
    port = int(ad.get("handler_port") or os.environ.get("XOSO66_DEPOSIT_HANDLER_PORT") or 5000)
    callback = str(
        ad.get("callback_url")
        or os.environ.get("XOSO66_DEPOSIT_CALLBACK_URL", f"http://127.0.0.1:{port}/callback")
    ).strip()
    return third, callback, port


THIRD_PARTY_API_URL, CALLBACK_URL, HANDLER_PORT = _load_urls()


def refresh_urls() -> None:
    """Đọc lại config (port/callback_url) trước mỗi lệnh nạp."""
    global THIRD_PARTY_API_URL, CALLBACK_URL, HANDLER_PORT
    THIRD_PARTY_API_URL, CALLBACK_URL, HANDLER_PORT = _load_urls()

app = Flask(__name__)
_tracking: set[int] = set()
_tracking_lock = threading.Lock()


def _resolve_account(account_id: str = "", username: str = "") -> dict[str, Any] | None:
    from xoso66_accounts_db import get_account, get_account_by_username

    aid = (account_id or "").strip()
    user = (username or "").strip()
    if aid:
        return get_account(aid)
    if user:
        return get_account_by_username(user)
    return None


def _qr_to_base64(
    qr_path: str = "",
    *,
    transfer_info: dict[str, Any] | None = None,
) -> str:
    """QR gửi bên thứ 3 — từ file/transfer_info cổng, không tải pay_url (HTML)."""
    from xoso66_deposit import qr_image_to_data_uri

    ti = transfer_info if isinstance(transfer_info, dict) else {}
    src = str(ti.get("source_url") or "").strip()
    hdrs = {"User-Agent": "Mozilla/5.0", "referer": src} if src else None
    return qr_image_to_data_uri(
        qr_path=qr_path,
        transfer_info=ti,
        http_headers=hdrs,
    )


def _bank_fields_from_deposit_result(result: dict[str, Any]) -> dict[str, str]:
    """STK / bank / NDCK từ kết quả create_xoso66_deposit_order."""
    from xoso66_deposit import transfer_info_bank_fields

    ti = result.get("transfer_info") if isinstance(result.get("transfer_info"), dict) else {}
    bf = transfer_info_bank_fields(ti)
    return {
        "account_number": str(result.get("account_number") or bf["account_number"]).strip(),
        "account_holder": str(result.get("account_holder") or bf["account_holder"]).strip(),
        "bank": str(result.get("bank") or bf["bank"]).strip(),
        "transfer_content": str(
            result.get("transfer_content") or bf["transfer_content"]
        ).strip(),
    }


def create_xoso66_deposit_order(account_id: str, amount: int) -> dict[str, Any]:
    from xoso66_accounts_db import init_db
    from xoso66_deposit import create_deposit_order
    from xoso66_deposit_orders_db import create_deposit_order_row
    from xoso66_session import ensure_session

    init_db()
    acc = _resolve_account(account_id=account_id)
    if not acc:
        return {"ok": False, "error": "không tìm thấy account"}

    from xoso66_accounts_db import username_for_log

    aid = str(acc["id"])
    username = username_for_log(aid, acc)
    since_ms = int(time.time() * 1000)

    try:
        session = ensure_session(aid, force_login=False)
    except Exception as e:
        return {"ok": False, "error": f"session: {e}"}

    dep = create_deposit_order(aid, int(amount), session=session)
    if not dep.get("ok"):
        return {"ok": False, "error": dep.get("error") or "tạo đơn nạp thất bại", "raw": dep}

    ti = dep.get("transfer_info") if isinstance(dep.get("transfer_info"), dict) else {}
    trade_no = str(dep.get("trade_no") or "")
    pay_url = str(dep.get("pay_url") or "")
    from xoso66_deposit import (
        fetch_qrpay_transfer_info,
        fetch_transfer_info_from_pay_url,
        save_qr_image,
        transfer_info_bank_fields,
        transfer_info_has_qr,
        _should_use_qrpay_api,
    )

    ch_id = int(dep.get("channel_id") or session.get("channel_id") or 0)
    if not transfer_info_bank_fields(ti).get("account_number"):
        if trade_no and _should_use_qrpay_api(trade_no, pay_url, ch_id):
            try:
                ti = fetch_qrpay_transfer_info(trade_no, session=session)
            except Exception as e:
                print(f"[CREATE-DEPOSIT] getWUInfo {trade_no}: {e}", flush=True)
        elif pay_url:
            try:
                ti = fetch_transfer_info_from_pay_url(
                    pay_url, amount=int(amount), session=session
                )
            except Exception as e:
                print(f"[CREATE-DEPOSIT] pay_url CK: {e}", flush=True)
        if dep.get("transfer_info_error"):
            print(
                f"[CREATE-DEPOSIT] transfer_info lần đầu: {dep.get('transfer_info_error')}",
                flush=True,
            )
    elif pay_url and not transfer_info_has_qr(ti):
        try:
            extra = fetch_transfer_info_from_pay_url(
                pay_url, amount=int(amount), session=session
            )
            if isinstance(extra, dict):
                for k, v in extra.items():
                    if v and not ti.get(k):
                        ti[k] = v
                for k in ("qr_data_uri", "qr_url", "qr_base64", "qr_emv_payload", "source_url"):
                    if extra.get(k):
                        ti[k] = extra[k]
        except Exception as e:
            print(f"[CREATE-DEPOSIT] bổ sung QR từ pay_url: {e}", flush=True)

    from xoso66_deposit import transfer_info_bank_fields

    bank_fields = transfer_info_bank_fields(ti)

    qr_path = str(dep.get("qr_image_path") or "")
    use_vietqr_fb = bool(trade_no and _should_use_qrpay_api(trade_no, pay_url, ch_id))
    try:
        saved = save_qr_image(
            ti,
            aid,
            session=session,
            allow_vietqr_fallback=use_vietqr_fb,
        )
        if saved:
            qr_path = saved
    except Exception as e:
        print(f"[CREATE-DEPOSIT] lưu QR: {e}", flush=True)

    qr_b64 = _qr_to_base64(qr_path, transfer_info=ti)
    if not qr_b64:
        print(
            f"[CREATE-DEPOSIT] [{user}] không có QR hợp lệ từ cổng — "
            f"bên thứ 3 có thể báo «mã QR không hợp lệ»",
            flush=True,
        )

    local_id = create_deposit_order_row(
        account_id=aid,
        username=username,
        amount=int(amount),
        serial_no="",
        trade_no=str(dep.get("trade_no") or ""),
        status="Đã tạo đơn",
        order_placed_at_ms=since_ms,
        qr_image_path=qr_path,
        transfer_info=ti,
    )

    return {
        "ok": True,
        "order_id": local_id,
        "account_id": aid,
        "username": username,
        "amount": int(amount),
        "order_placed_at_ms": since_ms,
        "trade_no": dep.get("trade_no"),
        "qr_base64": qr_b64,
        "qr_image_path": qr_path,
        "pay_url": dep.get("pay_url"),
        "transfer_info": ti,
        "qr_link": str(ti.get("qr_url") or "").strip(),
        "account_number": bank_fields["account_number"],
        "account_holder": bank_fields["account_holder"],
        "bank": bank_fields["bank"],
        "transfer_content": bank_fields["transfer_content"],
    }


def _normalize_callback_status(raw: Any) -> str:
    """Chuẩn hóa status callback bên thứ 3 → Đã Nạp / Thất Bại / Huỷ."""
    s = str(raw or "").strip()
    low = s.lower().replace("_", " ")
    if low in (
        "đã nạp",
        "da nap",
        "danap",
        "success",
        "succeeded",
        "completed",
        "complete",
        "done",
        "ok",
    ):
        return "Đã Nạp"
    if low in ("thất bại", "that bai", "fail", "failed", "error", "failure"):
        return "Thất Bại"
    if low in (
        "huỷ",
        "hủy",
        "huy",
        "đã hủy",
        "da huy",
        "cancel",
        "cancelled",
        "canceled",
    ) or any(k in low for k in ("huỷ", "hủy", "cancel")):
        return "Huỷ"
    return s


def _third_party_status_from_body(data: dict[str, Any]) -> str:
    """Đọc status từ JSON trả về POST /api/deposit (top-level hoặc data.*)."""
    if not isinstance(data, dict):
        return ""
    for key in ("status", "Status"):
        if data.get(key):
            return _normalize_callback_status(data[key])
    inner = data.get("data")
    if isinstance(inner, dict):
        for key in ("status", "Status"):
            if inner.get(key):
                return _normalize_callback_status(inner[key])
    for key in ("error", "message"):
        if data.get(key):
            st = _normalize_callback_status(data[key])
            if st in ("Huỷ", "Thất Bại", "Đã Nạp"):
                return st
    return ""


def _release_deposit_tracking(account_id: str, order_id: int | None = None) -> None:
    """Huỷ / thất bại bên thứ 3 — xóa cache nạp, dừng poll để acc lấy lệnh mới."""
    aid = str(account_id).strip()
    try:
        from xoso66_auto_deposit import release_deposit_reserve, remove_from_deposit_cache

        remove_from_deposit_cache(aid)
        release_deposit_reserve(aid, clear_cache=True)
    except Exception:
        pass
    try:
        from xoso66_ws_pool import release_ws_blocks_after_deposit

        release_ws_blocks_after_deposit([aid])
    except Exception:
        pass
    if order_id is not None:
        try:
            from xoso66_deposit_tracking import end_deposit_poll

            end_deposit_poll(int(order_id))
        except Exception:
            pass
        with _tracking_lock:
            _tracking.discard(int(order_id))


def send_to_third_party(username: str, amount: int, order_data: dict) -> dict[str, Any]:
    refresh_urls()
    order_id = order_data.get("order_id")
    qr_base64 = order_data.get("qr_base64", "")
    ti = order_data.get("transfer_info") if isinstance(order_data.get("transfer_info"), dict) else {}
    from xoso66_deposit import transfer_info_bank_fields

    fields = transfer_info_bank_fields(ti)
    acc_no = order_data.get("account_number") or fields["account_number"]
    holder = order_data.get("account_holder") or fields["account_holder"]
    bank = order_data.get("bank") or fields["bank"]
    ndck = (
        str(order_data.get("transfer_content") or fields["transfer_content"] or "")
        .strip()
        or str(order_id)
    )
    qr_link = str(ti.get("qr_url") or order_data.get("qr_link") or "").strip()
    if not qr_link:
        pay = str(order_data.get("pay_url") or "").strip()
        if pay.lower().split("?")[0].endswith((".png", ".jpg", ".jpeg", ".webp")):
            qr_link = pay

    if isinstance(qr_base64, str) and qr_base64 and not qr_base64.startswith("data:image"):
        qr_base64 = f"data:image/png;base64,{qr_base64.lstrip()}"

    payload: dict[str, Any] = {
        "orderId": str(order_id),
        "qrBase64": qr_base64,
        "username": username,
        "amount": amount,
        "accountNumber": acc_no,
        "accountHolder": holder,
        "bank": bank,
        "qrLink": qr_link,
        "qrImagePath": order_data.get("qr_image_path") or "",
        "receiver": acc_no,
        "name": holder,
        "type": bank,
        "transferContent": ndck,
        "msg": ndck,
        "callbackUrl": CALLBACK_URL,
        "callback_url": CALLBACK_URL,
    }
    try:
        resp = requests.post(THIRD_PARTY_API_URL, json=payload, timeout=30)
        data = resp.json() if resp.content else {}
        if not isinstance(data, dict):
            data = {}
        tp_status = _third_party_status_from_body(data)
        if tp_status == "Huỷ":
            err = str(data.get("error") or data.get("message") or "Bên thứ 3 huỷ lệnh")
            return {
                "ok": False,
                "error": err,
                "status": "Huỷ",
                "cancelled_by_third_party": True,
            }
        if resp.ok and data.get("ok"):
            return {
                "ok": True,
                "transaction_id": (data.get("data") or {}).get("orderId", ""),
                "message": data.get("message", ""),
            }
        err = str(data.get("error") or resp.text[:200] or "third party error")
        if _normalize_callback_status(err) == "Huỷ":
            return {
                "ok": False,
                "error": err,
                "status": "Huỷ",
                "cancelled_by_third_party": True,
            }
        return {"ok": False, "error": err, "status": tp_status or ""}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _run_poll_after_third_party(order_id: int) -> None:
    from xoso66_deposit_orders_db import get_deposit_order, update_deposit_order
    from xoso66_deposit_tracking import (
        deposit_order_confirmed,
        end_deposit_poll,
        poll_deposit_until_confirmed,
        try_begin_deposit_poll,
    )
    from xoso66_config_util import load_config
    from xoso66_session import ensure_session

    poll_acquired = False
    try:
        row = get_deposit_order(order_id)
        if not row:
            return
        if deposit_order_confirmed(order_id):
            from xoso66_accounts_db import username_for_log

            print(
                f"[DEPOSIT-POLL] #{order_id} [{username_for_log(str(row['account_id']), row)}] "
                f"đã Thành Công — bỏ poll (WS-POOL hoặc lần trước)",
                flush=True,
            )
            return
        if not try_begin_deposit_poll(order_id):
            if deposit_order_confirmed(order_id):
                return
            from xoso66_accounts_db import username_for_log

            print(
                f"[DEPOSIT-POLL] #{order_id} [{username_for_log(str(row['account_id']), row)}] "
                f"bỏ poll — đơn đang poll ở luồng khác (WS-POOL hoặc handler)",
                flush=True,
            )
            return

        poll_acquired = True
        aid = str(row["account_id"])
        amount = int(row.get("amount") or 0)
        since_ms = int(row.get("order_placed_at_ms") or 0)

        cfg = load_config()
        ad = cfg.get("auto_deposit") if isinstance(cfg.get("auto_deposit"), dict) else {}
        interval = float(ad.get("poll_interval_sec") or 10)
        max_attempts = int(ad.get("poll_max_attempts") or 100)
        list_limit = int(ad.get("deposit_list_limit") or 10)

        try:
            session = ensure_session(aid, force_login=False)
        except Exception as e:
            if not deposit_order_confirmed(order_id):
                update_deposit_order(order_id, status="Thất Bại", error_message=str(e))
            return

        from xoso66_accounts_db import username_for_log

        user = username_for_log(aid, row)
        print(
            f"[DEPOSIT-POLL] #{order_id} [{user}] {amount:,}đ — "
            f"list {list_limit} lệnh / {interval:.0f}s × {max_attempts}",
            flush=True,
        )
        rep = poll_deposit_until_confirmed(
            session,
            account_id=aid,
            amount_vnd=amount,
            since_ms=since_ms,
            poll_interval_sec=interval,
            max_attempts=max_attempts,
            list_limit=list_limit,
            order_id=order_id,
        )

        if rep.get("success"):
            via = str(rep.get("via") or "")
            if via in ("order_already_thanh_cong", "already_in_db"):
                print(
                    f"[DEPOSIT-POLL] ✅ #{order_id} [{user}] Hoàn tất "
                    f"(đã xác nhận — {via})",
                    flush=True,
                )
            else:
                item = rep.get("item") if isinstance(rep.get("item"), dict) else {}
                serial = str(rep.get("serial_no") or item.get("serial_no") or "")
                if not deposit_order_confirmed(order_id):
                    update_deposit_order(
                        order_id,
                        status="Thành Công",
                        serial_no=serial,
                        site_status=1,
                        site_status_formatted=str(
                            item.get("status_formatted") or "Hoàn tất"
                        ),
                    )
                print(
                    f"[DEPOSIT-POLL] ✅ #{order_id} [{user}] Hoàn tất "
                    f"(mới lưu DB serial={serial})",
                    flush=True,
                )
            try:
                from xoso66_session import refresh_account_balance_to_db

                bal_rep = refresh_account_balance_to_db(aid, session)
                if bal_rep.get("ok") and bal_rep.get("balance") is not None:
                    print(
                        f"[DEPOSIT-POLL]   balance DB={float(bal_rep['balance']):,.0f}",
                        flush=True,
                    )
                elif not bal_rep.get("ok"):
                    print(
                        f"[DEPOSIT-POLL]   balance: {bal_rep.get('error')}",
                        flush=True,
                    )
            except Exception as e:
                print(f"[DEPOSIT-POLL]   balance: {e}", flush=True)
            try:
                from xoso66_auto_deposit import remove_from_deposit_cache

                remove_from_deposit_cache(aid)
            except Exception:
                pass
            try:
                from xoso66_ws_pool import open_ws_after_deposit_confirmed

                open_ws_after_deposit_confirmed([aid], cfg)
            except Exception as e:
                print(
                    f"[DEPOSIT-POLL]   mở WS sau nạp: {e}",
                    flush=True,
                )
        else:
            if deposit_order_confirmed(order_id):
                print(
                    f"[DEPOSIT-POLL] #{order_id} [{user}] đã Thành Công — "
                    f"bỏ ghi Thất Bại (poll song song)",
                    flush=True,
                )
            else:
                update_deposit_order(
                    order_id,
                    status="Thất Bại",
                    error_message=str(rep.get("error") or "chưa thấy Hoàn tất mới"),
                )
                print(f"[DEPOSIT-POLL] ❌ #{order_id} [{user}] {rep.get('error')}", flush=True)
            try:
                from xoso66_auto_deposit import remove_from_deposit_cache

                remove_from_deposit_cache(aid)
            except Exception:
                pass
    finally:
        if poll_acquired:
            end_deposit_poll(order_id)
        with _tracking_lock:
            _tracking.discard(order_id)


@app.route("/callback", methods=["POST"])
def receive_callback() -> Any:
    data = request.json or {}
    order_id = data.get("order_id") or data.get("orderId")
    status_raw = data.get("status")
    status = _normalize_callback_status(status_raw)
    username = data.get("username", "")

    print(
        f"[CALLBACK] nhận order={order_id} status={status_raw!r}→{status!r} user={username}",
        flush=True,
    )

    if not order_id:
        return jsonify({"error": "Missing order_id"}), 400
    if not status_raw:
        return jsonify({"error": "Missing status"}), 400

    transfer_content = str(
        data.get("transferContent") or data.get("transfer_content") or ""
    ).strip()

    try:
        game = resolve_callback_game(order_id, username, transfer_content)
        if game == "lc79":
            print(
                f"[CALLBACK] bỏ qua #{order_id} [{username}] — đơn LC79 (handler :5000)",
                flush=True,
            )
            return jsonify({"success": False, "skipped": "lc79_order"}), 404
    except Exception as e:
        print(f"[CALLBACK] routing check lỗi: {e}", flush=True)

    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        return jsonify({"error": "order_id invalid"}), 400

    from xoso66_deposit_orders_db import get_deposit_order, update_deposit_order

    row = get_deposit_order(oid)
    if not row:
        print(
            f"[CALLBACK] bỏ qua #{order_id} — không có trong DB XOSO66",
            flush=True,
        )
        return jsonify({"success": False, "skipped": "not_in_xoso66_db"}), 404

    if status == "Đã Nạp":
        if str(row.get("status") or "") == "Thành Công":
            return jsonify({"success": True, "skipped": "already_thanh_cong"}), 200

        prev_st = str(row.get("status") or "").strip()
        if prev_st != "Đã Nạp":
            update_deposit_order(oid, status="Đã Nạp")

        from xoso66_deposit_tracking import deposit_poll_in_progress

        if deposit_poll_in_progress(oid):
            print(
                f"[CALLBACK] #{oid} [{username}] Đã Nạp — "
                f"DB {prev_st!r}→'Đã Nạp' (WS-POOL/handler đang poll, không mở poll mới)",
                flush=True,
            )
            return jsonify({"success": True, "skipped": "poll_in_progress"}), 200
        with _tracking_lock:
            if oid in _tracking:
                return jsonify({"success": True, "skipped": "already_tracking"}), 200
            _tracking.add(oid)
        threading.Thread(
            target=_run_poll_after_third_party,
            args=(oid,),
            daemon=True,
        ).start()
        print(f"[CALLBACK] #{oid} [{username}] Đã Nạp → bắt đầu poll", flush=True)
    elif status in ("Thất Bại", "Huỷ"):
        if str(row.get("status") or "") == "Thành Công":
            return jsonify(
                {"success": True, "skipped": "already_thanh_cong", "status": "Thành Công"}
            ), 200
        update_deposit_order(oid, status=str(status))
        aid_cb = str(row.get("account_id") or "")
        _release_deposit_tracking(aid_cb, oid)
        if status == "Huỷ":
            print(
                f"[CALLBACK] #{oid} [{username}] Huỷ — xóa cache, dừng poll "
                f"(bên thứ 3 không nạp)",
                flush=True,
            )
        else:
            print(
                f"[CALLBACK] #{oid} [{username}] Thất Bại — xóa cache",
                flush=True,
            )
    else:
        update_deposit_order(oid, status=str(status))

    return jsonify({"success": True, "order_id": oid, "status": status}), 200


@app.route("/create-deposit", methods=["POST"])
def create_deposit() -> Any:
    try:
        return _create_deposit_impl()
    except Exception as e:
        import traceback

        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


def _create_deposit_impl() -> Any:
    data = request.json or {}
    account_id = str(data.get("account_id") or data.get("accountId") or "").strip()
    username = str(data.get("username") or "").strip()
    amount = int(data.get("amount") or 0)

    acc = _resolve_account(account_id=account_id, username=username)
    if not acc:
        return jsonify({"error": "Account not found"}), 404
    from xoso66_accounts_db import username_for_log

    aid = str(acc["id"])
    user = username_for_log(aid, acc)
    if amount <= 0:
        from xoso66_config_util import load_config

        ad = load_config().get("auto_deposit")
        if isinstance(ad, dict):
            amount = int(ad.get("amount_vnd") or 100_000)
        else:
            amount = 100_000

    try:
        from xoso66_auto_deposit import release_deposit_reserve, try_reserve_deposit

        if not try_reserve_deposit(aid):
            return jsonify({"error": "Đang có lệnh nạp chưa xong"}), 409
    except Exception:
        return jsonify({"error": "Không kiểm tra được trạng thái nạp"}), 503

    try:
        result = create_xoso66_deposit_order(aid, amount)
    except Exception as e:
        try:
            from xoso66_auto_deposit import release_deposit_reserve

            release_deposit_reserve(aid, clear_cache=True)
        except Exception:
            pass
        return jsonify({"ok": False, "error": str(e)}), 500

    if not result.get("ok"):
        try:
            from xoso66_auto_deposit import release_deposit_reserve

            release_deposit_reserve(aid, clear_cache=True)
        except Exception:
            pass
        try:
            print(
                f"[CREATE-DEPOSIT] FAIL [{user}] {amount:,}đ — {result.get('error')}",
                flush=True,
            )
        except UnicodeEncodeError:
            print(
                f"[CREATE-DEPOSIT] FAIL [{user}] {amount:,} VND — {result.get('error')}",
                flush=True,
            )
        return jsonify(result), 400

    order_id = int(result["order_id"])

    try:
        from xoso66_auto_deposit import release_deposit_reserve

        release_deposit_reserve(aid)
    except Exception:
        pass

    ti = result.get("transfer_info") if isinstance(result.get("transfer_info"), dict) else {}
    bank_fields = _bank_fields_from_deposit_result(result)
    tp = send_to_third_party(user, amount, result)
    if not tp.get("ok"):
        from xoso66_deposit_orders_db import update_deposit_order

        err_tp = str(tp.get("error") or "third party")
        cancelled = bool(tp.get("cancelled_by_third_party")) or tp.get("status") == "Huỷ"
        st_tp = "Huỷ" if cancelled else "Thất Bại"
        print(
            f"[CREATE-DEPOSIT] #{order_id} [{user}] bên thứ 3 {st_tp}: {err_tp}",
            flush=True,
        )
        update_deposit_order(
            order_id,
            status=st_tp,
            error_message=err_tp,
        )
        _release_deposit_tracking(aid, order_id)
        return jsonify(
            {
                "ok": False,
                "error": err_tp,
                "order_id": order_id,
                "account_id": aid,
                "username": user,
                "amount": amount,
                "trade_no": result.get("trade_no"),
                "transfer_info": ti,
                "bank": bank_fields["bank"],
                "account_number": bank_fields["account_number"],
                "account_holder": bank_fields["account_holder"],
                "transfer_content": bank_fields["transfer_content"],
                "pay_url": result.get("pay_url") or "",
                "qr_image_path": result.get("qr_image_path") or "",
                "third_party_error": err_tp,
            }
        ), 500

    from xoso66_deposit_orders_db import update_deposit_order

    update_deposit_order(
        order_id,
        status="Chờ bên thứ 3",
        third_party_tx_id=str(tp.get("transaction_id") or ""),
    )

    from xoso66_config_util import load_config

    ad_cfg = load_config().get("auto_deposit")
    if isinstance(ad_cfg, dict) and ad_cfg.get("poll_on_third_party_ok"):
        with _tracking_lock:
            if order_id not in _tracking:
                _tracking.add(order_id)
                threading.Thread(
                    target=_run_poll_after_third_party,
                    args=(order_id,),
                    daemon=True,
                ).start()

    ok_body = {
        "ok": True,
        "order_id": order_id,
        "account_id": aid,
        "username": user,
        "amount": amount,
        "trade_no": result.get("trade_no"),
        "transaction_id": tp.get("transaction_id"),
        "status": "PENDING",
        "message": "Đã gửi bên thứ 3 — chờ callback Đã Nạp rồi poll lịch sử",
        "transfer_info": ti,
        "bank": bank_fields["bank"],
        "account_number": bank_fields["account_number"],
        "account_holder": bank_fields["account_holder"],
        "transfer_content": bank_fields["transfer_content"],
        "pay_url": result.get("pay_url") or "",
        "qr_image_path": result.get("qr_image_path") or "",
        "qr_base64": (result.get("qr_base64") or "")[:80],
    }
    from xoso66_auto_deposit import log_deposit_order_compact

    print(flush=True)
    log_deposit_order_compact(ok_body)
    return jsonify(ok_body), 200


@app.route("/health", methods=["GET"])
def health() -> Any:
    return jsonify(
        {
            "status": "running",
            "callback_url": CALLBACK_URL,
            "third_party_url": THIRD_PARTY_API_URL,
        }
    )


def run_handler(host: str = "0.0.0.0", port: int | None = None) -> None:
    """Werkzeug server — shutdown() khi Ctrl+C (không treo như app.run)."""
    from werkzeug.serving import make_server

    from xoso66_shutdown import clear_deposit_handler, register_deposit_handler, stopping

    from xoso66_config_util import load_config, startup_quiet

    configure_stdio_utf8()
    refresh_urls()
    p = int(port or HANDLER_PORT)
    if not startup_quiet(load_config()):
        print(
            f"[XOSO66-DEPOSIT-HANDLER] :{p} | callback={CALLBACK_URL} | "
            f"third_party={THIRD_PARTY_API_URL}",
            flush=True,
        )
    httpd = make_server(host, p, app, threaded=True)
    register_deposit_handler(httpd)
    try:
        httpd.serve_forever()
    finally:
        clear_deposit_handler()
        if stopping():
            print("[DEPOSIT-HANDLER] Đã dừng.", flush=True)


if __name__ == "__main__":
    run_handler()
