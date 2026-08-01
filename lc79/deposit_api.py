def deposit_full_process(username: str, amount: int) -> dict:
    """
    Thực hiện đầy đủ quy trình nạp tiền:
    - Gọi deposit một lần
    - Bổ sung STK/NDCK từ QR nếu API che hoặc thiếu
    - Chỉ lưu DB khi đủ 5 trường: ngân hàng, STK, chủ TK, số tiền, NDCK
  """
    blocked = pending_deposit_error(username)
    if blocked:
        print(
            f"⚠️ [{username}] Bỏ tạo lệnh nạp — đang có lệnh #{blocked.get('orderId')} chưa hoàn thành",
            flush=True,
        )
        return blocked

    result = deposit(username, amount)
    payload = (result.get("data") or {}).get("data") or {} if result.get("data") else {}
    api_data = result.get("data") or {}

    if not result.get("ok"):
        err = result.get("error", "API lỗi")
        print(f"⚠️ [{username}] Deposit lỗi: {err} (amount={amount})", flush=True)
        _log_api_response(username, 1, result, payload, api_data)
        return result

    if not payload:
        api_error = api_data.get("message", "API không trả dữ liệu")
        api_code = api_data.get("code", "?")
        print(f"⚠️ [{username}] API không trả dữ liệu (amount={amount})", flush=True)
        _log_api_response(username, 1, result, payload, api_data)
        return {"ok": False, "error": f"[{api_code}] {api_error}"}

    _enrich_payload_from_qr(payload, api_data)
    fields = extract_deposit_fields(payload, api_data, amount)
    ok_fields, field_err = validate_deposit_fields(fields)
    if not ok_fields:
        print(f"❌ [{username}] Lệnh nạp thiếu thông tin — không lưu DB: {field_err}", flush=True)
        _log_api_response(username, 1, result, payload, api_data)
        return {"ok": False, "error": field_err, "missing_fields": fields}

    effective_amount = int(fields["amount"])
    save_result = save_deposit_to_db(username, result, amount=effective_amount, fields=fields)
    if not save_result.get("ok"):
        err = save_result.get("error") or "Lưu DB thất bại"
        out = {"ok": False, "error": err}
        if save_result.get("order"):
            out["order"] = save_result["order"]
            out["orderId"] = save_result["order"].get("id")
        return out

    saved = save_result.get("ok")
    order_id = save_result.get("orderId")
    img_path = save_qr_image(payload, username)
    return {
        "ok": True,
        "message": "Tạo lệnh nạp tiền thành công (full process)",
        "data": {
            "username": username,
            "amount": effective_amount,
            "accountNumber": fields["account_number"],
            "bank": fields["bank"],
            "accountHolder": fields["account_holder"],
            "transferContent": fields["transfer_content"],
            "qrBase64": payload.get('qr_base64') or payload.get('qr', ''),
            "qrLink": payload.get('qr_link', ''),
            "qrImagePath": img_path,
            "savedToDB": saved,
            "orderId": order_id
        }
    }
import sys
import io
import os

# Disable buffering cho CMS
os.environ['PYTHONUNBUFFERED'] = '1'

# Fix encoding cho Windows console
if sys.platform == 'win32':
    os.system('chcp 65001 > nul')

import os, re, base64, requests
from game_api_helper import game_request_with_retry

# Dùng cấu hình chung nếu có, fallback localhost
try:
    from fetch_transactions import NODE_SERVER_URL
except Exception:
    NODE_SERVER_URL = "http://127.0.0.1:3000"

DEPOSIT_URL = "https://gameapi.tele68.com/v1/payment-app/cash-in/bank"
from constants import REPO_ROOT

QR_DIR = str(REPO_ROOT / "qr_outputs")

_PENDING_DEPOSIT_STATUSES = frozenset({
    "Chờ Nạp", "Đang Nạp", "Đã Nạp", "pending", "PENDING",
})
_TERMINAL_DEPOSIT_STATUSES = frozenset({
    "Thành Công", "Thất Bại", "Huỷ", "Hủy",
})


def get_pending_deposit_order(username: str) -> dict | None:
    """Lệnh nạp chưa kết thúc trên Node DB (mới nhất), hoặc None."""
    u = (username or "").strip()
    if not u:
        return None
    try:
        resp = requests.get(
            f"{NODE_SERVER_URL}/api/deposit-orders",
            params={"username": u, "limit": 20},
            timeout=5,
        )
        if resp.status_code != 200:
            return None
        data = resp.json() or {}
        orders = data.get("data") if isinstance(data.get("data"), list) else (
            data if isinstance(data, list) else []
        )
        for order in orders:
            if not isinstance(order, dict):
                continue
            status = (order.get("status") or order.get("Status") or "").strip()
            if not status or status in _TERMINAL_DEPOSIT_STATUSES:
                continue
            if status in _PENDING_DEPOSIT_STATUSES or status:
                return order
        return None
    except Exception as e:
        print(f"⚠️ Không kiểm tra được lệnh nạp treo của [{u}]: {e}", flush=True)
        return None


def pending_deposit_error(username: str) -> dict | None:
    """Trả về payload lỗi 409 nếu user đang có lệnh nạp treo."""
    pending = get_pending_deposit_order(username)
    if not pending:
        return None
    return {
        "ok": False,
        "error": "Tài khoản đang có lệnh nạp chưa hoàn thành",
        "order": pending,
        "orderId": pending.get("id"),
    }

def _ensure_qr_dir():
    os.makedirs(QR_DIR, exist_ok=True)

def _sanitize_filename(text: str) -> str:
    s = (text or "").strip()
    s = re.sub(r'[\\/:*?"<>|]', "_", s)   # ký tự cấm Windows
    s = re.sub(r"\s+", "_", s)            # khoảng trắng -> _
    return (s[:80] or "no_msg")           # giới hạn độ dài

def _log_api_response(username: str, attempt: int, result: dict, payload: dict, api_data: dict):
    """In dữ liệu API nhận được khi thất bại (không có NDCK) để debug."""
    import json
    # Copy để không in base64 (quá dài)
    def _sanitize_for_log(d):
        if not d:
            return d
        out = {}
        for k, v in d.items():
            if k in ("qr", "qr_base64", "qrBase64") and isinstance(v, str):
                out[k] = f"<base64 {len(v)} chars>" if v else v
            elif isinstance(v, dict):
                out[k] = _sanitize_for_log(v)
            else:
                out[k] = v
        return out
    to_log = {"ok": result.get("ok"), "error": result.get("error")}
    if api_data:
        to_log["data"] = _sanitize_for_log(api_data)
    print(f"   📥 [{username}] Lần {attempt} - API trả về: {json.dumps(to_log, ensure_ascii=False)[:1200]}", flush=True)

def save_qr_image(payload: dict, username: str) -> str | None:
    """
    Lưu ảnh QR ra PNG với tên: username_NDCK.png
    Ưu tiên base64; fallback tải từ qr_link.
    """
    _ensure_qr_dir()
    safe_msg = _sanitize_filename(_get_transfer_content(payload))
    filename = f"{username}_{safe_msg}.png"
    out_path = os.path.join(QR_DIR, filename)

    # 1) Base64 trước
    b64 = payload.get("qr") or payload.get("qr_base64")
    if b64:
        try:
            if isinstance(b64, str) and b64.startswith("data:image"):
                b64 = b64.split(",", 1)[1]
            with open(out_path, "wb") as f:
                f.write(base64.b64decode(b64))
            return out_path
        except Exception:
            pass

    # 2) Fallback: tải từ qr_link
    qr_link = payload.get("qr_link")
    if qr_link:
        try:
            r = requests.get(qr_link, timeout=20)
            if r.ok:
                with open(out_path, "wb") as f:
                    f.write(r.content)
                return out_path
        except Exception:
            pass

    return None

def update_deposit_order_status(order_id: int, status: str, device_nap: str | None = None) -> bool:
    """
    Cập nhật trạng thái lệnh nạp tiền trong DB.
    
    Args:
        order_id: ID của lệnh nạp trong deposit-orders
        status: Trạng thái mới ("Chờ Nạp"|"Đang Nạp"|"Đã Nạp"|"Thành Công"|"Thất Bại"|"Huỷ")
        device_nap: Tên device thực hiện chuyển khoản (tuỳ chọn)
    """
    try:
        payload: dict = {"status": status}
        if device_nap and str(device_nap).strip():
            payload["deviceNap"] = str(device_nap).strip()
        r = requests.put(
            f"{NODE_SERVER_URL}/api/deposit-orders/{order_id}",
            json=payload,
            timeout=5
        )
        return r.status_code in (200, 204)
    except Exception as e:
        print(f"⚠️ Lỗi cập nhật trạng thái order: {e}")
        return False


def deposit(username: str, amount: int) -> dict:

    if not username or amount <= 0:
        return {"ok": False, "error": "Thiếu username hoặc amount không hợp lệ"}

    # Build params cho API nạp tiền
    params = {"amount": int(amount)}

    # Bỏ log tạo lệnh nạp
    try:
        resp = game_request_with_retry(username, "GET", DEPOSIT_URL, params=params, timeout=30)

        if not resp:
            print(f"❌ [{username}] Không nhận được response từ API", flush=True)
            return {"ok": False, "error": "Không gọi được API nạp tiền"}

        result = {"ok": resp.ok, "status": resp.status_code}
        try:
            result["data"] = resp.json()
        except Exception as e:
            print(f"⚠️ [{username}] Không parse được JSON: {e}", flush=True)
            result["text"] = resp.text

        return result

    except Exception as e:
        print(f"❌ [{username}] Lỗi khi gọi deposit API: {e}")
        import traceback
        traceback.print_exc()
        return {"ok": False, "error": str(e)}

# Các giá trị KHÔNG phải NDCK (thông báo API), bị bỏ qua khi lấy NDCK
_NDCK_EXCLUDE = frozenset(s.lower() for s in (
    "success", "ok", "successful", "thành công", "Thành công", "THÀNH CÔNG",
    "fail", "failed", "error", "lỗi", "pending", "processing"
))

def _get_transfer_content(payload: dict, api_data: dict = None) -> str:
    """Lấy NDCK từ payload. Hỗ trợ msg, transferContent (format BC...). Bỏ qua message/content là thông báo API."""
    for d in (payload, api_data or {}):
        if not d:
            continue
        for key in ("msg", "transferContent", "transfer_content", "message", "content"):
            val = d.get(key)
            if val and isinstance(val, str):
                val = val.strip()
                if not val:
                    continue
                # Bỏ qua nếu là thông báo API (success, ok, ...) không phải NDCK
                if val.lower() in _NDCK_EXCLUDE:
                    continue
                return val
    return ""


def _is_masked_account(value: str | None) -> bool:
    """True nếu STK trống hoặc bị cổng thanh toán che (*********)."""
    s = (value or "").strip()
    if not s:
        return True
    return bool(re.fullmatch(r"\*+", s))


def _parse_account_from_emv(emv: str) -> str | None:
    """Số TK từ payload VietQR EMV (NAPAS AID A000000727 → tag 01)."""
    emv = str(emv or "").strip()
    if not emv.startswith("000201"):
        return None
    m = re.search(r"A00000072701(\d{2})(\d+)", emv)
    if not m:
        return None
    ln = int(m.group(1))
    payload = m.group(2)[:ln]
    m2 = re.search(r"01(\d{2})(\d+)", payload)
    if m2:
        aln = int(m2.group(1))
        acct = m2.group(2)[:aln]
        if acct.isdigit() and 6 <= len(acct) <= 20:
            return acct
    runs = re.findall(r"\d{8,16}", payload)
    return runs[-1] if runs else None


def _silence_opencv_logs() -> None:
    """Tắt WARN OpenCV (ECI is not supported properly khi decode VietQR)."""
    try:
        import cv2

        logging = getattr(getattr(cv2, "utils", None), "logging", None)
        if logging is not None and hasattr(logging, "setLogLevel"):
            # LOG_LEVEL_ERROR = 3; fallback số để tương thích bản OpenCV cũ
            level = getattr(logging, "LOG_LEVEL_ERROR", 3)
            logging.setLogLevel(level)
        elif hasattr(cv2, "setLogLevel"):
            cv2.setLogLevel(0)
    except Exception:
        pass


def _decode_qr_emv_from_base64(b64: str) -> str | None:
    """Decode ảnh QR (base64) → chuỗi EMV VietQR. Upscale nếu ảnh nhỏ."""
    if not b64 or not isinstance(b64, str):
        return None
    try:
        raw_b64 = b64.split(",", 1)[1] if b64.startswith("data:image") else b64
        raw = base64.b64decode(raw_b64)
    except Exception:
        return None
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    _silence_opencv_logs()
    try:
        arr = np.frombuffer(raw, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return None
        det = cv2.QRCodeDetector()
        candidates = [img]
        # Ảnh QR nhúng từ cổng đôi khi rất nhỏ (~100–200px) — upscale nhiều mức
        h, w = img.shape[:2]
        max_side = max(h, w)
        for fx in (2, 3, 4, 6):
            if max_side * fx < 200:
                continue
            if max_side >= 500 and fx > 2:
                break
            candidates.append(
                cv2.resize(img, None, fx=fx, fy=fx, interpolation=cv2.INTER_NEAREST)
            )
        # Thử grayscale — một số QR nhúng decode ổn hơn khi bỏ màu
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        candidates.append(gray)
        if max_side < 400:
            candidates.append(
                cv2.resize(gray, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST)
            )
        for candidate in candidates:
            val, _, _ = det.detectAndDecode(candidate)
            if val and str(val).startswith("000201"):
                return str(val)
            if val:
                return str(val)
    except Exception as e:
        print(f"⚠️ Decode QR EMV thất bại: {e}", flush=True)
    return None


def _parse_emv_tlvs(data: str) -> dict[str, str]:
    """Parse chuỗi TLV EMV VietQR → {tag: value}."""
    tags: dict[str, str] = {}
    i = 0
    data = str(data or "")
    while i + 4 <= len(data):
        tag = data[i : i + 2]
        try:
            ln = int(data[i + 2 : i + 4])
        except ValueError:
            break
        i += 4
        val = data[i : i + ln]
        i += ln
        tags[tag] = val
    return tags


def _parse_transfer_content_from_emv(emv: str) -> str | None:
    """NDCK từ EMV VietQR (tag 62 → sub-tag 08/01)."""
    emv = str(emv or "").strip()
    if not emv.startswith("000201"):
        return None
    top = _parse_emv_tlvs(emv)
    add = top.get("62")
    if not add:
        return None
    sub = _parse_emv_tlvs(add)
    for key in ("08", "01", "07"):
        val = str(sub.get(key) or "").strip()
        if val and val.lower() not in _NDCK_EXCLUDE:
            return val
    return None


def _download_qr_base64(qr_link: str) -> str:
    """Tải ảnh QR từ link → data URI base64."""
    link = str(qr_link or "").strip()
    if not link:
        return ""
    try:
        r = requests.get(link, timeout=20)
        if r.ok and r.content:
            return f"data:image/png;base64,{base64.b64encode(r.content).decode()}"
    except Exception:
        pass
    return ""


def _qr_base64_from_payload(payload: dict) -> str:
    """Lấy base64 QR từ payload; fallback tải qr_link."""
    b64 = str(payload.get("qr") or payload.get("qr_base64") or "").strip()
    if b64:
        return b64
    return _download_qr_base64(str(payload.get("qr_link") or ""))


def _parse_account_from_vietqr_link(qr_link: str) -> str | None:
    """
    STK từ URL VietQR dạng:
      https://img.vietqr.io/image/{binOrBank}-{stk}-qr_only.png?...
    """
    link = str(qr_link or "").strip()
    if not link:
        return None
    m = re.search(
        r"vietqr\.io/image/([^/?#]+)-([0-9]{6,20})-(?:qr_only|compact|print)",
        link,
        re.IGNORECASE,
    )
    if m:
        return m.group(2)
    m = re.search(r"/image/[^/?#]*-([0-9]{6,20})-", link)
    if m:
        return m.group(1)
    return None


def _get_emv_from_payload(payload: dict) -> str | None:
    """
    Decode ảnh QR → chuỗi EMV.
    Ưu tiên base64 trong payload; nếu OpenCV không đọc được thì tải qr_link
    (ảnh nhúng đôi khi quá nhỏ / lỗi ECI).
    Cache trên payload để tránh decode lặp trong cùng 1 flow.
    """
    cached = payload.get("_emv_cache")
    if isinstance(cached, str) and cached.startswith("000201"):
        return cached

    emv = None
    b64 = str(payload.get("qr") or payload.get("qr_base64") or "").strip()
    if b64:
        emv = _decode_qr_emv_from_base64(b64)
    if not emv:
        qr_link = str(payload.get("qr_link") or "").strip()
        if qr_link:
            link_b64 = _download_qr_base64(qr_link)
            if link_b64:
                emv = _decode_qr_emv_from_base64(link_b64)
    if emv:
        payload["_emv_cache"] = emv
    return emv


def _enrich_payload_from_qr(payload: dict, api_data: dict | None = None) -> None:
    """Bổ sung STK/NDCK từ QR / qr_link khi API che hoặc thiếu."""
    emv = _get_emv_from_payload(payload)

    receiver = str(payload.get("receiver") or "").strip()
    if _is_masked_account(receiver):
        acct = _parse_account_from_emv(emv) if emv else None
        if not acct:
            acct = _parse_account_from_vietqr_link(str(payload.get("qr_link") or ""))
            if acct:
                print(f"🔓 STK bị che trên API — lấy từ qr_link: {acct}", flush=True)
        elif acct:
            print(f"🔓 STK bị che trên API — lấy từ QR: {acct}", flush=True)
        if acct:
            payload["receiver"] = acct

    ndck = _get_transfer_content(payload, api_data)
    if not ndck and emv:
        from_emv = _parse_transfer_content_from_emv(emv)
        if from_emv:
            print(f"🔓 NDCK thiếu trên API — lấy từ QR: {from_emv}", flush=True)
            payload["msg"] = from_emv


def _resolve_account_number(payload: dict) -> str:
    """
    Lấy STK nhận: ưu tiên receiver từ API game.
    Nếu cổng che (*********) thì decode từ QR VietQR.
    Ghi đè lại payload['receiver'] khi recover được để tránh decode lặp.
    """
    _enrich_payload_from_qr(payload)
    receiver = str(payload.get("receiver") or "").strip()
    if not _is_masked_account(receiver):
        return receiver
    emv = _get_emv_from_payload(payload)
    acct = _parse_account_from_emv(emv) if emv else None
    if acct:
        print(f"🔓 STK bị che trên API — lấy từ QR: {acct}", flush=True)
        payload["receiver"] = acct
        return acct
    return receiver


def extract_deposit_fields(payload: dict, api_data: dict | None, amount: int) -> dict:
    """Chuẩn hóa 5 trường bắt buộc trước khi lưu DB / gửi bên thứ 3."""
    _enrich_payload_from_qr(payload, api_data)
    return {
        "bank": str(payload.get("type") or "").strip(),
        "account_number": _resolve_account_number(payload),
        "account_holder": str(payload.get("name") or "").strip(),
        "amount": int(amount or 0),
        "transfer_content": _get_transfer_content(payload, api_data),
    }


def validate_deposit_fields(fields: dict) -> tuple[bool, str]:
    """True nếu đủ 5 trường: ngân hàng, STK, chủ TK, số tiền, NDCK."""
    bank = str(fields.get("bank") or "").strip()
    account_number = str(fields.get("account_number") or "").strip()
    account_holder = str(fields.get("account_holder") or "").strip()
    amount = int(fields.get("amount") or 0)
    transfer_content = str(fields.get("transfer_content") or "").strip()

    missing = []
    if not bank:
        missing.append("ngân hàng")
    if not account_holder:
        missing.append("chủ TK")
    if amount <= 0:
        missing.append("số tiền")
    if not transfer_content:
        missing.append("NDCK")
    if _is_masked_account(account_number):
        missing.append("STK (bị che hoặc trống)")
    elif not (account_number.isdigit() and 6 <= len(account_number) <= 20):
        missing.append("STK (không hợp lệ)")

    if missing:
        return False, "Thiếu thông tin nạp: " + ", ".join(missing)
    return True, ""


def resolve_account_number_for_send(
    account_number: str | None,
    *,
    qr_base64: str | None = None,
    qr_image_path: str | None = None,
    qr_link: str | None = None,
) -> str:
    """Decode STK đầy đủ trước khi gửi bên thứ 3."""
    acc = str(account_number or "").strip()
    if not _is_masked_account(acc):
        return acc

    payload: dict = {}
    b64 = str(qr_base64 or "").strip()
    if not b64 and qr_image_path and os.path.exists(qr_image_path):
        try:
            with open(qr_image_path, "rb") as f:
                b64 = f"data:image/png;base64,{base64.b64encode(f.read()).decode()}"
        except Exception:
            pass
    if not b64 and qr_link:
        payload["qr_link"] = qr_link
    if b64:
        payload["qr_base64"] = b64
    _enrich_payload_from_qr(payload)
    resolved = str(payload.get("receiver") or "").strip()
    if resolved and not _is_masked_account(resolved):
        print(f"🔓 STK bị che — decode từ QR trước khi gửi bên thứ 3: {resolved}", flush=True)
        return resolved
    return acc


def save_deposit_to_db(username: str, api_result: dict, status: str = "pending", amount: int = None, fields: dict | None = None) -> dict:
    """
    Lưu lệnh nạp tiền vào DB với trạng thái pending.
    
    Returns:
        dict: {ok: bool, orderId: int} - orderId để tracking sau này
    """
    api_data = api_result.get("data", {}) or {}
    payload = api_data.get("data", {}) or {}
    if fields is None:
        fields = extract_deposit_fields(payload, api_data, amount or 0)
    ok_fields, field_err = validate_deposit_fields(fields)
    if not ok_fields:
        print(f"⚠️ [{username}] Không lưu DB — {field_err}", flush=True)
        return {"ok": False, "error": field_err, "missing_fields": fields}

    ndck = fields["transfer_content"]
    rec = {
        "username": username,
        "amount": fields["amount"],
        "accountNumber": fields["account_number"],
        "bank": fields["bank"],
        "accountHolder": fields["account_holder"],
        "transferContent": ndck,
        "transfer_content": ndck,  # Node/Prisma có thể dùng snake_case cho cột DB
    }
    try:
        # Log để debug: NDCK format BC... (có dấu -) có thể bị Node/DB từ chối
        if ndck and ("-" in ndck or ndck.startswith("BC")):
            print(f"📤 Lưu DB: transferContent={ndck[:50]}... (len={len(ndck)})", flush=True)
        r = requests.post(f"{NODE_SERVER_URL}/api/deposit-orders", json=rec, timeout=5)
        if r.status_code in (200, 201):
            data = r.json()
            return {"ok": True, "orderId": data.get("id")}
        body = {}
        try:
            body = r.json() if r.text else {}
        except Exception:
            body = {}
        print(f"⚠️ Lưu DB thất bại - status {r.status_code}: {r.text}", flush=True)
        out = {"ok": False, "status_code": r.status_code}
        if isinstance(body, dict):
            out["error"] = body.get("error") or f"Lưu DB thất bại (HTTP {r.status_code})"
            if body.get("order"):
                out["order"] = body["order"]
        else:
            out["error"] = f"Lưu DB thất bại (HTTP {r.status_code})"
        return out
    except Exception as e:
        print(f"⚠️ Lỗi lưu DB: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return {"ok": False}

if __name__ == "__main__":
    import json
    
    # Nếu có arguments từ command line -> mode API (trả JSON)
    if len(sys.argv) >= 3:
        try:
            username = sys.argv[1]
            amount = int(sys.argv[2])
            result = deposit(username, amount)
            if not result.get("ok"):
                print(json.dumps(result, ensure_ascii=False))
                sys.exit(1)
            payload = result.get("data", {}).get("data", {}) or {}
            if not payload:
                api_error = result.get("data", {}).get("message", "API không trả dữ liệu")
                api_code = result.get("data", {}).get("code", "?")
                error_result = {
                    "ok": False,
                    "error": f"[{api_code}] {api_error}"
                }
                print(json.dumps(error_result, ensure_ascii=False))
                sys.exit(1)
            api_data = result.get("data", {}) or {}
            _enrich_payload_from_qr(payload, api_data)
            fields = extract_deposit_fields(payload, api_data, amount)
            ok_fields, field_err = validate_deposit_fields(fields)
            if not ok_fields:
                print(json.dumps({"ok": False, "error": field_err, "missing_fields": fields}, ensure_ascii=False))
                sys.exit(1)
            # Lưu DB và QR
            save_result = save_deposit_to_db(username, result, amount=amount, fields=fields)
            saved = save_result.get("ok")
            order_id = save_result.get("orderId")
            img_path = save_qr_image(payload, username)
            account_number = fields["account_number"]
            transfer_content = fields["transfer_content"]
            # In log đẹp với icon
            print()
            print(f"🎮 User: {username}", flush=True)
            print(f"👤 Tên TK: {fields['account_holder']}", flush=True)
            print(f"🏦 Số TK: {account_number}", flush=True)
            print(f"💰 Số tiền: {amount:,} đ", flush=True)
            print(f"📝 Nội dung: \033[1;31m{transfer_content}\033[0m", flush=True)
            # Bỏ log order_id
            print()
            # Trả kết quả JSON
            success_result = {
                "ok": True,
                "message": "Tạo lệnh nạp tiền thành công",
                "data": {
                    "username": username,
                    "amount": amount,
                    "accountNumber": account_number,
                    "bank": fields["bank"],
                    "accountHolder": fields["account_holder"],
                    "transferContent": transfer_content,
                    "qrBase64": payload.get('qr_base64') or payload.get('qr', ''),
                    "qrLink": payload.get('qr_link', ''),
                    "qrImagePath": img_path,
                    "savedToDB": saved,
                    "orderId": order_id
                }
            }
            print(json.dumps(success_result, ensure_ascii=False), flush=True)
            sys.exit(0)
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False), flush=True)
            sys.exit(1)
    
    # Mode interactive (không có arguments)
    else:
        u = input("Username: ").strip()
        a = int(input("Amount: ").strip() or "0")
        
        result = deposit(u, a)

        if not result.get("ok"):
            print(f"❌ Lỗi: {result.get('error', 'Unknown error')}", flush=True)
        else:
            payload = result.get("data", {}).get("data", {}) or {}
            
            if not payload:
                api_error = result.get("data", {}).get("message", "API không trả dữ liệu")
                api_code = result.get("data", {}).get("code", "?")
                print(f"❌ Lỗi API: [{api_code}] {api_error}", flush=True)
            else:
                api_data = result.get("data", {}) or {}
                _enrich_payload_from_qr(payload, api_data)
                fields = extract_deposit_fields(payload, api_data, a)
                ok_fields, field_err = validate_deposit_fields(fields)
                if not ok_fields:
                    print(f"❌ {field_err}", flush=True)
                else:
                    save_result = save_deposit_to_db(u, result, amount=a, fields=fields)
                    saved = save_result.get("ok")
                    order_id = save_result.get("orderId")
                    
                    img_path = save_qr_image(payload, u)
                    account_number = fields["account_number"]
                    if not img_path:
                        print("❌ Không lấy được ảnh QR (thiếu base64 và qr_link).", flush=True)
                    else:
                        print("✅ Nạp thành công (đã lưu lệnh pending).", flush=True)
                        print(f"   Username: {u}", flush=True)
                        print(f"   STK nhận: {account_number}", flush=True)
                        print(f"   Ngân hàng: {fields['bank']}", flush=True)
                        print(f"   Tên: {fields['account_holder']}", flush=True)
                        print(f"   NDCK: {fields['transfer_content']}", flush=True)
                        print(f"   Ảnh QR: {img_path}", flush=True)
                        print(f"   Lưu DB: {'OK' if saved else 'Lỗi lưu'}", flush=True)
