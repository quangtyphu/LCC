#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XOSO66 — tạo đơn nạp QRPay + lấy STK/nội dung CK (độc lập, không dùng LC79).

Chạy thử:
  python xoso66_deposit.py -a acc1 -m 100000

Cần file cùng thư mục:
  xoso66_sessions.json   — cookie/session (mẫu: xoso66_sessions.example.json)
  Mã hóa: xoso66_crypto.py (đã port từ index.js — pip install pycryptodome)
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

try:
    from xoso66_session import _requests_session as _game_http_session
except ImportError:
    _game_http_session = None  # type: ignore


def _game_http(session: dict):
    """requests.Session qua SOCKS5 — bắt buộc proxy."""
    if _game_http_session is None:
        raise RuntimeError("xoso66_session không import được")
    return _game_http_session(session)


try:
    from xoso66_crypto import (
        decrypt_response as _py_decrypt,
        encrypt_payload as _py_encrypt,
        random_aes_key,
    )

    _PY_CRYPTO = True
except ImportError:
    _PY_CRYPTO = False

# ── Cấu hình site (sửa nếu domain đổi) ─────────────────────────────────────
BASE_URL = os.environ.get("XOSO66_BASE_URL", "https://v6sgqpyi.whskxk1.com").rstrip("/")
DEPOSIT_ORDER_PATH = "/server/payment/depositorder"
PAYMENT_ORDER_LIST_PATH = "/server/payment/paymentorderlist"
QRPAY_BASE = os.environ.get("XOSO66_QRPAY_BASE", "https://pay.qrpay.quest").rstrip("/")
QRPAY_CHANNEL_ID = 235
QRPAY_GET_WU_INFO_PATH = "/prod-api/pay/page/PayAccount/getWUInfo"
_DEPOSIT_PLAYWRIGHT_ACTIONS = ("userCenter/depositorder",)
_HTTP_DEPOSIT_FALLBACK_CODES = (1004, 10055, 10058)

DIR = Path(__file__).resolve().parent
QR_OUTPUT_DIR = DIR / "qr_outputs"
from xoso66_sessions_io import SESSIONS_FILE, load_sessions, save_sessions  # noqa: E402
CRYPTO_IMPL_JS = Path(os.environ.get("XOSO66_CRYPTO_JS", DIR / "xoso66_crypto_impl.js"))
CRYPTO_RUNNER_JS = DIR / "_xoso66_crypto_runner.js"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

DEPOSIT_INFO_PATH = "/server/payment/depositinfo"

# ── Sessions ─────────────────────────────────────────────────────────────────

def _cookie_header(session: dict) -> str:
    cookies = session.get("cookies") or {}
    if isinstance(cookies, str):
        return cookies.strip()
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v is not None)


def build_common_headers(session: dict, *, form_token: str, content_type: str) -> dict[str, str]:
    extra = session.get("headers") or {}
    h = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "vi,en-US;q=0.9,en;q=0.8",
        "content-type": content_type,
        "origin": BASE_URL,
        "referer": f"{BASE_URL}/home/",
        "user-agent": session.get("user_agent") or DEFAULT_UA,
        "x-device": "pc",
        "x-lang": "vi",
        "x-theme": "dark",
        "x-app-platform": "undefined",
        "cookie": _cookie_header(session),
        "form-token": form_token,
        "c-a-i": extra.get("c-a-i") or session.get("c_a_i") or "",
        "cf-auth-token": extra.get("cf-auth-token") or session.get("cf_auth_token") or "",
        "cf-con-s": extra.get("cf-con-s") or session.get("cf_con_s") or "",
        "cf-pass": extra.get("cf-pass") or session.get("cf_pass") or "",
    }
    for k, v in extra.items():
        if v and k.lower() not in ("cookie",):
            h[k] = str(v)
    return {k: v for k, v in h.items() if v}


def build_request_headers(session: dict, *, cek_k: str, form_token: str) -> dict[str, str]:
    """Header khi POST depositorder (co ma hoa)."""
    h = build_common_headers(session, form_token=form_token, content_type="application/text")
    if cek_k:
        h["cek-k"] = cek_k
    return h


# ── Crypto (Node + xoso66_crypto_impl.js do bạn cung cấp) ─────────────────────

_CRYPTO_RUNNER_SOURCE = r"""
const fs = require('fs');
const path = require('path');
const implPath = path.join(__dirname, 'xoso66_crypto_impl.js');
if (!fs.existsSync(implPath)) {
  console.error(JSON.stringify({ ok: false, error: 'MISSING_CRYPTO_IMPL' }));
  process.exit(2);
}
const impl = require(implPath);
const [,, op, payload, cekK] = process.argv;
try {
  let out;
  if (op === 'encrypt') {
    const plain = JSON.parse(payload);
    out = impl.encrypt(plain, cekK || '');
  } else if (op === 'decrypt') {
    out = impl.decrypt(payload, cekK || '');
  } else {
    throw new Error('op must be encrypt|decrypt');
  }
  console.log(JSON.stringify({ ok: true, data: out }));
} catch (e) {
  console.error(JSON.stringify({ ok: false, error: String(e.message || e) }));
  process.exit(1);
}
"""


def _ensure_crypto_runner() -> None:
    if not CRYPTO_RUNNER_JS.is_file():
        CRYPTO_RUNNER_JS.write_text(_CRYPTO_RUNNER_SOURCE, encoding="utf-8")


def crypto_available() -> bool:
    return _PY_CRYPTO or CRYPTO_IMPL_JS.is_file()


def node_crypto(op: str, payload: Any, cek_k: str = "") -> Any:
    """op: encrypt | decrypt. encrypt: payload=dict. decrypt: payload=str cipher."""
    _ensure_crypto_runner()
    if not crypto_available():
        raise RuntimeError(
            f"Thiếu {CRYPTO_IMPL_JS.name} — copy hàm encrypt/decrypt từ index.*.js của XOSO66.\n"
            "Chạy: python xoso66_deposit.py --help-setup"
        )
    if op == "encrypt":
        arg = json.dumps(payload, ensure_ascii=False)
    else:
        arg = payload if isinstance(payload, str) else str(payload)
    proc = subprocess.run(
        ["node", str(CRYPTO_RUNNER_JS), op, arg, cek_k or ""],
        cwd=str(DIR),
        capture_output=True,
        text=True,
        timeout=30,
    )
    raw = (proc.stdout or proc.stderr or "").strip()
    if not raw:
        raise RuntimeError(f"Node crypto không trả output (code={proc.returncode})")
    try:
        result = json.loads(raw.splitlines()[-1])
    except json.JSONDecodeError:
        result = json.loads(proc.stdout.strip())
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "crypto failed"))
    return result["data"]


def prepare_deposit_payload(
    amount: int,
    *,
    merchant_id: int | str,
    random_remark: str = "",
    form_token: str = "",
    bank_id: int | str = "",
    extra: dict | None = None,
) -> dict:
    """
    Payload depositorder (tu chunk charge Vue):
    merchant_id, money, random_remark, form_token, bank_id
  — KHONG co channel_id trong body.
    """
    body: dict[str, Any] = {
        "merchant_id": int(merchant_id),
        "money": str(int(amount)),
        "random_remark": str(random_remark or ""),
        "form_token": str(form_token or ""),
        "bank_id": str(bank_id) if bank_id not in (None, "") else "",
    }
    if extra:
        body.update(extra)
    return body


def qrpay_merchant_from_deposit_info(info: dict) -> tuple[int, int, str]:
    """QRPay-nạp tiền bankking (channel 235) — merchant_id + random_remark."""
    for cat in info.get("payment_list") or []:
        for ch in cat.get("channel") or []:
            if ch.get("id") != QRPAY_CHANNEL_ID:
                continue
            merchants = ch.get("merchant") or []
            if not merchants:
                break
            m = merchants[0]
            return (
                int(ch["id"]),
                int(m["id"]),
                str(m.get("random_remark") or ""),
            )
    raise ValueError(f"Khong tim thay kenh QRPay (channel_id={QRPAY_CHANNEL_ID})")


def resolve_qrpay_params(session: dict) -> tuple[int, int, str]:
    """merchant_id + random_remark cho QRPay (cache neu da dung kenh 235)."""
    if (
        session.get("merchant_id")
        and session.get("random_remark") is not None
        and int(session.get("channel_id") or 0) == QRPAY_CHANNEL_ID
    ):
        return (
            QRPAY_CHANNEL_ID,
            int(session["merchant_id"]),
            str(session.get("random_remark") or ""),
        )
    info = get_deposit_info(session=session)
    if not info.get("ok"):
        raise ValueError(f"depositinfo loi: {info.get('raw')}")
    return qrpay_merchant_from_deposit_info(info)


def fetch_cek_p(session: dict) -> str:
    """Header cek-p (RSA public key) — tu response API, luu trong session."""
    if session.get("cek_p"):
        return str(session["cek_p"])
    url = f"{BASE_URL}/server/index/encryptKey"
    r = _game_http(session).get(
        url,
        headers={
            "user-agent": session.get("user_agent") or DEFAULT_UA,
            "cookie": _cookie_header(session),
            "accept": "application/json",
        },
        timeout=20,
    )
    cek_p = r.headers.get("cek-p") or r.headers.get("Cek-P") or ""
    if not cek_p:
        raise ValueError(
            "Khong lay duoc cek-p. Them 'cek_p' vao session (copy response header sau login/refresh trang)."
        )
    session["cek_p"] = cek_p
    return cek_p


def get_aes_session_key(session: dict) -> str:
    if session.get("aes_session_key"):
        return str(session["aes_session_key"])
    if not _PY_CRYPTO:
        raise RuntimeError("Can pycryptodome: pip install pycryptodome")
    key = random_aes_key()
    session["aes_session_key"] = key
    return key


def encrypt_deposit_body(session: dict, plain: dict) -> tuple[str, str, str]:
    """Tra (body, cek-k header, aes_key)."""
    if _PY_CRYPTO:
        aes_key = get_aes_session_key(session)
        cek_p = fetch_cek_p(session)
        body, cek_k = _py_encrypt(plain, aes_key, cek_p)
        return body, cek_k, aes_key
    cek_k = str(session.get("cek_k") or "")
    body = node_crypto("encrypt", plain, cek_k)
    if isinstance(body, dict):
        body = body.get("body") or body.get("cipher") or json.dumps(body)
    return str(body).strip(), cek_k, cek_k


def decrypt_deposit_body(session: dict, cipher: str, aes_key: str, resp_headers: dict) -> Any:
    cek_s = resp_headers.get("cek-s") or resp_headers.get("Cek-S")
    if cek_s and str(cek_s) != "1" and not _PY_CRYPTO:
        pass
    if _PY_CRYPTO:
        new_p = resp_headers.get("cek-p") or resp_headers.get("Cek-P")
        if new_p:
            session["cek_p"] = new_p
        new_ft = resp_headers.get("form-token") or resp_headers.get("Form-Token")
        if new_ft:
            session["form_token"] = new_ft
        return _py_decrypt(cipher, aes_key)
    return node_crypto("decrypt", cipher, aes_key)


def apply_response_tokens(session: dict, headers: Any) -> None:
    """Cap nhat form-token / cek-p tu response header."""
    if hasattr(headers, "get"):
        new_ft = headers.get("form-token") or headers.get("Form-Token")
        new_p = headers.get("cek-p") or headers.get("Cek-P")
    else:
        new_ft = new_p = None
    if new_ft:
        session["form_token"] = new_ft
    if new_p:
        session["cek_p"] = new_p


def get_form_token(session: dict) -> str:
    if session.get("form_token"):
        return str(session["form_token"])
    # Thử lấy từ trang charge (một số site nhúng trong HTML)
    try:
        r = _game_http(session).get(
            f"{BASE_URL}/home/",
            headers={
                "user-agent": session.get("user_agent") or DEFAULT_UA,
                "cookie": _cookie_header(session),
            },
            timeout=20,
        )
        m = re.search(r'form-token["\']?\s*[:=]\s*["\']([^"\']+)', r.text, re.I)
        if m:
            return m.group(1)
        m = re.search(r"formToken['\"]?\s*[:=]\s*['\"]([^'\"]+)", r.text, re.I)
        if m:
            return m.group(1)
    except Exception:
        pass
    raise ValueError(
        "Thiếu form_token: thêm 'form_token' trong session (copy header form-token khi nạp — đổi nhanh)"
    )


# ── depositinfo (GET — danh sach kenh nap) ───────────────────────────────────

def get_deposit_info(account_id: str | None = None, session: dict | None = None) -> dict:
    """GET /payment/depositinfo — tra payment_list, last_deposit, fast_money."""
    if session is None:
        sessions = load_sessions()
        if not account_id:
            raise ValueError("Can account_id")
        session = sessions[account_id]
    form_token = get_form_token(session)
    headers = build_common_headers(session, form_token=form_token, content_type="application/json")
    url = f"{BASE_URL}{DEPOSIT_INFO_PATH}"
    r = _game_http(session).get(url, headers=headers, timeout=30)
    apply_response_tokens(session, r.headers)
    try:
        js = r.json()
    except Exception:
        return {"ok": False, "http_status": r.status_code, "raw": r.text[:500]}
    data = js.get("data") or {}
    last = data.get("last_deposit") or {}
    return {
        "ok": r.status_code == 200 and js.get("code") == 1,
        "payment_list": data.get("payment_list"),
        "last_deposit": last,
        "fast_money": data.get("fast_money"),
        "suggested_channel_id": last.get("channel_id"),
        "suggested_merchant_id": last.get("merchant_id"),
        "raw": js,
    }


def apply_qrpay_to_session(session: dict, info: dict | None = None) -> dict:
    """Gan merchant_id / random_remark QRPay tu depositinfo."""
    if info is None:
        info = get_deposit_info(session=session)
    try:
        ch_id, mer_id, remark = qrpay_merchant_from_deposit_info(info)
        session["channel_id"] = ch_id
        session["merchant_id"] = mer_id
        session["random_remark"] = remark
        session["bank_id"] = ""
    except ValueError:
        pass
    return session


# ── paymentorderlist (JSON thuong — khong ma hoa) ────────────────────────────

def list_payment_orders(
    account_id: str | None = None,
    session: dict | None = None,
    *,
    order_type: int = 1,
    status: str = "-1",
    page: int = 1,
    limit: int = 10,
    start_time_ms: int | None = None,
    end_time_ms: int | None = None,
) -> dict:
    """
    Lich su don nap/rut. order_type=1 la nap. status=-1 la tat ca.
    API nay KHONG ma hoa — chi can cookie + form-token + cf headers.
    """
    if session is None:
        sessions = load_sessions()
        if not account_id:
            raise ValueError("Can account_id hoac session")
        session = sessions[account_id]

    now_ms = int(time.time() * 1000)
    if end_time_ms is None:
        end_time_ms = now_ms
    if start_time_ms is None:
        start_time_ms = end_time_ms - 7 * 24 * 3600 * 1000

    body = {
        "type": order_type,
        "status": str(status),
        "page": page,
        "limit": limit,
        "startTime": start_time_ms,
        "endTime": end_time_ms,
    }
    form_token = get_form_token(session)
    headers = build_common_headers(
        session,
        form_token=form_token,
        content_type="application/x-www-form-urlencoded/json",
    )
    url = f"{BASE_URL}{PAYMENT_ORDER_LIST_PATH}"
    r = _game_http(session).post(
        url,
        data=json.dumps(body, separators=(",", ":")),
        headers=headers,
        timeout=30,
    )
    try:
        js = r.json()
    except Exception:
        return {"ok": False, "http_status": r.status_code, "raw": r.text[:500]}
    ok = r.status_code == 200 and js.get("code") == 1
    return {
        "ok": ok,
        "http_status": r.status_code,
        "msg": js.get("msg"),
        "data": js.get("data"),
        "list": (js.get("data") or {}).get("list") or [],
        "raw": js,
    }


# ── depositorder ─────────────────────────────────────────────────────────────

def post_deposit_order(
    session: dict,
    encrypted_body: str,
    *,
    cek_k: str,
    form_token: str,
) -> tuple[int, str, dict]:
    url = f"{BASE_URL}{DEPOSIT_ORDER_PATH}"
    headers = build_request_headers(session, cek_k=cek_k, form_token=form_token)
    r = _game_http(session).post(url, data=encrypted_body, headers=headers, timeout=45)
    text = r.text
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1]
    return r.status_code, text, dict(r.headers)


def deposit_order_playwright(session: dict, plain: dict) -> dict[str, Any]:
    try:
        from xoso66_playwright_ctx import playwright_browser
    except ImportError:
        raise RuntimeError("pip install playwright && playwright install chromium")

    from xoso66_session import BASE_URL as _base, merge_playwright_cookies

    extra = {"accept": "application/json", "x-lang": "vi", "x-device": "pc"}
    for k in ("c-a-i", "cf-auth-token", "cf-con-s", "cf-pass"):
        v = (session.get("headers") or {}).get(k)
        if v:
            extra[k] = v
    ft = plain.get("form_token") or session.get("form_token")
    if ft:
        extra["form-token"] = ft

    js: Any = None
    with playwright_browser(session, base_url=_base, headless=True, extra_http_headers=extra) as (
        _p,
        _browser,
        context,
    ):
        page = context.new_page()
        page.goto(f"{_base}/home/", wait_until="domcontentloaded", timeout=90_000)
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            page.wait_for_timeout(6_000)
        page.wait_for_timeout(2_000)
        for action in _DEPOSIT_PLAYWRIGHT_ACTIONS:
            js = page.evaluate(
                """async ([action, body]) => {
                    const vm = document.querySelector('#app').__vue__;
                    try { return await vm.$store.dispatch(action, body); }
                    catch (e) { return { code: 0, msg: String(e) }; }
                }""",
                [action, plain],
            )
            if isinstance(js, dict) and js.get("code") == 1:
                break
        merge_playwright_cookies(session, context.cookies())

    if not isinstance(js, dict):
        return {"ok": False, "error": "response không hợp lệ", "raw": js, "method": "playwright"}
    return {"ok": js.get("code") == 1, "raw": js, "method": "playwright"}


def save_qr_image(transfer_info: dict, account_id: str, session: dict | None = None) -> str | None:
    """Lưu QR từ getWUInfo (base64 hoặc qr_url) → qr_outputs/."""
    QR_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    remark = (
        transfer_info.get("transfer_content")
        or transfer_info.get("merchant_order_no")
        or transfer_info.get("trade_no")
        or "deposit"
    )
    safe = re.sub(r"[^\w\-]", "_", str(remark))[:48]
    out_path = QR_OUTPUT_DIR / f"{account_id}_{safe}.png"

    uri = transfer_info.get("qr_data_uri")
    if isinstance(uri, str) and uri.startswith("data:"):
        try:
            b64 = uri.split(",", 1)[1]
            out_path.write_bytes(base64.b64decode(b64))
            return str(out_path)
        except Exception:
            pass

    url = transfer_info.get("qr_url")
    if url:
        try:
            r = _game_http(session or {}).get(str(url), timeout=25)
            if r.ok:
                out_path.write_bytes(r.content)
                return str(out_path)
        except Exception:
            pass

    # QRPay getWUInfo thường không trả ảnh — dựng VietQR từ STK + NDCK
    bank_map = {"VPBank": "VPB", "ACB": "ACB", "Vietcombank": "VCB", "Techcombank": "TCB"}
    bank_name = transfer_info.get("bank_name") or ""
    account_no = transfer_info.get("account_no") or ""
    content = transfer_info.get("transfer_content") or ""
    amount = transfer_info.get("amount")
    code = bank_map.get(str(bank_name), str(bank_name))
    if code and account_no and content and amount:
        from urllib.parse import quote

        vurl = (
            f"https://img.vietqr.io/image/{code}-{account_no}-compact2.jpg"
            f"?amount={int(float(amount))}&addInfo={quote(str(content))}"
        )
        try:
            r = _game_http(session or {}).get(vurl, timeout=25)
            if r.ok and r.content:
                out_path.write_bytes(r.content)
                return str(out_path)
        except Exception:
            pass
    return None


def parse_deposit_response(decrypted: Any) -> dict:
    """Chuẩn hóa object sau decrypt."""
    if isinstance(decrypted, str):
        try:
            decrypted = json.loads(decrypted)
        except json.JSONDecodeError:
            return {"raw": decrypted}
    if not isinstance(decrypted, dict):
        return {"raw": decrypted}
    data = decrypted.get("data") if isinstance(decrypted.get("data"), dict) else decrypted
    pay_url = (
        data.get("payUrl")
        or data.get("pay_url")
        or data.get("url")
        or data.get("redirectUrl")
        or data.get("link")  # TIMEPAY: action=jump
        or decrypted.get("payUrl")
    )
    trade_no = data.get("tradeNo") or data.get("trade_no") or data.get("orderNo")
    if pay_url and not trade_no:
        q = parse_qs(urlparse(str(pay_url)).query)
        trade_no = (q.get("tradeNo") or q.get("trade_no") or [None])[0]
    code = decrypted.get("code")
    return {
        "pay_url": pay_url,
        "trade_no": trade_no,
        "order_no": data.get("orderNo") or data.get("order_no"),
        "amount": data.get("amount") or data.get("money"),
        "action": data.get("action"),
        "success": code == 1,
        "raw": decrypted,
    }


def create_deposit_order(
    account_id: str,
    amount: int,
    *,
    session: dict | None = None,
    extra_plain: dict | None = None,
) -> dict:
    """
    Tạo đơn nạp QRPay trên XOSO66 + lấy STK/nội dung CK (getWUInfo).
    """
    sessions = load_sessions()
    session = session or sessions.get(account_id)
    if not session:
        raise KeyError(f"Không có account '{account_id}' trong sessions")

    try:
        from xoso66_session import ensure_session

        session = ensure_session(account_id)
        sessions[account_id] = session
    except Exception as e:
        return {"ok": False, "account_id": account_id, "error": f"Session/login: {e}"}

    if not session.get("merchant_id"):
        apply_qrpay_to_session(session)
    get_deposit_info(session=session)
    session.pop("aes_session_key", None)
    ch_id, mer_id, remark = resolve_qrpay_params(session)
    session["channel_id"] = ch_id
    session["merchant_id"] = mer_id
    session["random_remark"] = remark
    session["bank_id"] = ""

    form_token = get_form_token(session)
    plain = prepare_deposit_payload(
        amount,
        merchant_id=mer_id,
        random_remark=remark,
        form_token=form_token,
        bank_id="",
        extra=extra_plain or session.get("deposit_extra"),
    )

    if not crypto_available():
        raise RuntimeError("pip install pycryptodome (hoac them xoso66_crypto_impl.js)")

    encrypted_body, cek_k, aes_key = encrypt_deposit_body(session, plain)

    status, cipher_resp, resp_headers = post_deposit_order(
        session, encrypted_body, cek_k=cek_k, form_token=form_token
    )
    if status != 200:
        return {"ok": False, "error": f"HTTP {status}", "cipher_response": cipher_resp[:500]}

    method = "http"
    try:
        decrypted = decrypt_deposit_body(session, cipher_resp, aes_key, resp_headers)
    except Exception as e:
        decrypted = None
        if cipher_resp.strip().startswith("{"):
            try:
                decrypted = json.loads(cipher_resp)
            except json.JSONDecodeError:
                return {
                    "ok": False,
                    "error": f"Decrypt loi: {e}",
                    "cipher_response": cipher_resp[:300],
                }
        else:
            return {
                "ok": False,
                "error": f"Decrypt loi: {e}",
                "cipher_response": cipher_resp[:300],
            }

    parsed = parse_deposit_response(decrypted)
    raw_code = (parsed.get("raw") or {}).get("code")
    if not parsed.get("success") and raw_code in _HTTP_DEPOSIT_FALLBACK_CODES:
        pw = deposit_order_playwright(session, plain)
        method = pw.get("method", "playwright")
        parsed = parse_deposit_response(pw.get("raw") or pw)

    trade_no = parsed.get("trade_no")
    pay_url = parsed.get("pay_url")
    success = parsed.get("success") or bool(trade_no or pay_url)

    out = {
        "ok": success,
        "account_id": account_id,
        "amount": amount,
        "pay_url": pay_url,
        "trade_no": trade_no,
        "order_no": parsed.get("order_no"),
        "action": parsed.get("action"),
        "deposit_raw": parsed.get("raw"),
        "method": method,
    }

    if trade_no:
        try:
            out["transfer_info"] = fetch_qrpay_transfer_info(trade_no, session=session)
            qr_path = save_qr_image(out["transfer_info"], account_id, session=session)
            if qr_path:
                out["qr_image_path"] = qr_path
        except Exception as e:
            out["transfer_info_error"] = str(e)

    if not out["ok"]:
        raw = parsed.get("raw") or {}
        out["error"] = raw.get("msg") or "Tạo đơn thất bại — kiểm tra decrypt / payload / form_token"
    else:
        try:
            sessions[account_id] = session
            save_sessions(sessions)
        except Exception:
            pass
    return out


# ── QRPay: lấy STK / nội dung CK ─────────────────────────────────────────────


def transfer_info_bank_fields(ti: dict | None) -> dict[str, str]:
    """Map transfer_info (getWUInfo) → field gửi bên thứ 3 / log."""
    if not isinstance(ti, dict):
        return {
            "account_number": "",
            "account_holder": "",
            "bank": "",
            "transfer_content": "",
        }
    return {
        "account_number": str(
            ti.get("account_no")
            or ti.get("accountNumber")
            or ti.get("account")
            or ""
        ).strip(),
        "account_holder": str(
            ti.get("account_name")
            or ti.get("accountName")
            or ti.get("name")
            or ""
        ).strip(),
        "bank": str(
            ti.get("bank_name") or ti.get("bankName") or ti.get("bank") or ""
        ).strip(),
        "transfer_content": str(
            ti.get("transfer_content") or ti.get("remark") or ""
        ).strip(),
    }


def fetch_qrpay_transfer_info(
    trade_no: str,
    *,
    random_val: int | None = None,
    session: dict | None = None,
) -> dict:
    """POST getWUInfo — STK, remark, số tiền (sau khi có tradeNo)."""
    page_url = f"{QRPAY_BASE}/ePay?tradeNo={trade_no}"
    api = f"{QRPAY_BASE}{QRPAY_GET_WU_INFO_PATH}"
    if random_val is None:
        random_val = secrets.randbelow(100) + 1
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "vi,en-US;q=0.9,en;q=0.8",
        "content-type": "application/json;charset=UTF-8",
        "origin": QRPAY_BASE,
        "referer": page_url,
        "user-agent": DEFAULT_UA,
    }
    # QRPay host khác XOSO66 — gọi trực tiếp (không qua proxy game)
    r = requests.post(
        api,
        json={"tradeNo": trade_no, "random": random_val},
        headers=headers,
        timeout=25,
    )
    if r.status_code != 200:
        raise RuntimeError(f"QRPay HTTP {r.status_code}: {r.text[:300]}")
    try:
        js = r.json()
    except json.JSONDecodeError as e:
        raise RuntimeError(f"QRPay không phải JSON: {r.text[:200]}") from e
    if js.get("code") != 200:
        raise RuntimeError(js.get("msg") or f"QRPay code={js.get('code')}")
    info = _normalize_transfer_info(js)
    info["trade_no"] = trade_no
    info["source_url"] = api
    return info


def _normalize_transfer_info(js: Any) -> dict:
    if not isinstance(js, dict):
        return {}
    d = js.get("data") if isinstance(js.get("data"), dict) else js
    if isinstance(d, list) and d:
        d = d[0] if isinstance(d[0], dict) else {}
    return {
        "amount": d.get("amount") or d.get("money") or d.get("payAmount"),
        "bank_name": d.get("bankName") or d.get("bank") or d.get("bank_name") or d.get("bankCode"),
        "account_no": (
            d.get("accountNo")
            or d.get("accountNumber")
            or d.get("account")
            or d.get("bankAccount")
            or d.get("cardNo")
            or d.get("receiver")
        ),
        "account_name": (
            d.get("accountName")
            or d.get("name")
            or d.get("userName")
            or d.get("payName")
            or d.get("receiverName")
        ),
        "transfer_content": (
            d.get("transferContent")
            or d.get("descCode")
            or d.get("remark")
            or d.get("content")
            or d.get("note")
            or d.get("payRemark")
        ),
        "qr_url": d.get("qrUrl") or d.get("qr_url") or d.get("qrcode") or d.get("s3Url"),
        "qr_data_uri": d.get("qrCode") if isinstance(d.get("qrCode"), str) and d.get("qrCode", "").startswith("data:") else None,
        "merchant_order_no": d.get("merchatOrderNo") or d.get("merchantOrderNo") or d.get("systemOrderNo"),
        "expire_seconds": d.get("expireTime"),
        "status": d.get("statusStr"),
        "raw": js,
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def print_setup_help() -> None:
    print(
        """
═══ BẠN CẦN CUNG CẤP ═══

1) xoso66_sessions.json (bắt buộc) — mỗi acc một entry:
   - id: tên acc (acc1, user01...)
   - cookies: PHPSESSID, cf_clearance, __cf_bm, ...
   - form_token: copy header "form-token" lúc bấm Nạp ngay (đổi thường xuyên)
   - headers (tuỳ chọn): c-a-i, cf-auth-token, cf-con-s, cf-pass (copy từ curl depositorder)
   - headers (tuỳ chọn): c-a-i, cf-auth-token, cf-con-s, cf-pass

2) pip install -r requirements.txt  (xoso66_crypto.py)

3) Kênh nạp: chỉ QRPay-nạp tiền bankking (min 100k)

4) (Tuỳ chọn) cek_p trong session neu GET /index/encryptKey khong tra header

═══ KHONG CAN ═══
- xoso66_crypto_impl.js (da co Python)
- Node.js
"""
    )


def write_example_files() -> None:
    example_sessions = {
        "accounts": [
            {
                "id": "acc1",
                "cookies": {
                    "PHPSESSID": "THAY_BANG_PHPSESSID",
                    "cf_clearance": "THAY_BANG_CF_CLEARANCE",
                    "__cf_bm": "THAY_NEU_CO"
                },
                "form_token": "THAY_FORM_TOKEN.TIMESTAMP",
                "headers": {
                    "c-a-i": "THAY_CAI",
                    "cf-auth-token": "Bearer.xxx",
                    "cf-con-s": "THAY",
                    "cf-pass": "THAY"
                }
            }
        ]
    }
    ex = DIR / "xoso66_sessions.example.json"
    ex.write_text(json.dumps(example_sessions, indent=2, ensure_ascii=False), encoding="utf-8")

    crypto_ex = DIR / "xoso66_crypto_impl.example.js"
    crypto_ex.write_text(
        """// Đổi tên thành xoso66_crypto_impl.js và điền code thật từ web XOSO66
module.exports = {
  encrypt(plain, cekK) {
    // plain: object { amount, payType, channelId, ... }
    // return: string body gửi POST depositorder
    throw new Error('Chưa implement — copy từ index.*.js');
  },
  decrypt(cipher, cekK) {
    // cipher: response string từ server
    // return: object hoặc JSON string
    throw new Error('Chưa implement');
  },
  // generateCekK() { ... }  // optional
};
""",
        encoding="utf-8",
    )
    print(f"Da tao: {ex.name}, {crypto_ex.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="XOSO66 deposit standalone")
    parser.add_argument("--account", "-a", help="account id trong xoso66_sessions.json")
    parser.add_argument("--amount", "-m", type=int, default=100_000, help="số tiền VND")
    parser.add_argument("--deposit-info", action="store_true", help="goi GET depositinfo")
    parser.add_argument("--trade-no", help="chi lay CK QRPay (getWUInfo), bo qua tao don")
    parser.add_argument("--list-orders", action="store_true", help="goi paymentorderlist (lich su don)")
    parser.add_argument("--status", default="-1", help="loc status paymentorderlist (-1=tat ca)")
    parser.add_argument("--init", action="store_true", help="tạo file mẫu")
    parser.add_argument("--help-setup", action="store_true", help="hướng dẫn dữ liệu cần cung cấp")
    parser.add_argument("--check", action="store_true", help="kiểm tra sessions + crypto")
    parser.add_argument("--login", action="store_true", help="renew session (khi đã có xoso66_login)")
    args = parser.parse_args()

    if args.help_setup:
        print_setup_help()
        return 0
    if args.init:
        write_example_files()
        print_setup_help()
        return 0

    if args.check:
        print(f"sessions: {SESSIONS_FILE} -> {SESSIONS_FILE.is_file()}")
        print(f"crypto:   py={_PY_CRYPTO} js={CRYPTO_IMPL_JS.is_file()}")
        try:
            import Crypto  # noqa: F401
            print("pycryptodome: OK")
        except ImportError:
            print("pycryptodome: MISSING (pip install pycryptodome)")
        if SESSIONS_FILE.is_file():
            s = load_sessions()
            print(f"accounts: {list(s.keys())}")
        return 0

    if args.trade_no:
        info = fetch_qrpay_transfer_info(args.trade_no)
        print(json.dumps(info, indent=2, ensure_ascii=False))
        return 0

    if args.deposit_info:
        if not args.account:
            parser.error("--deposit-info can --account")
        sessions = load_sessions()
        result = get_deposit_info(args.account, sessions[args.account])
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    if args.list_orders:
        if not args.account:
            parser.error("--list-orders can --account")
        result = list_payment_orders(args.account, status=args.status)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    if args.login:
        if not args.account:
            parser.error("--login cần --account")
        from xoso66_session import ensure_session

        ensure_session(args.account, force_login=True)
        print(f"OK: session renewed → {SESSIONS_FILE}")
        return 0

    if not args.account:
        parser.print_help()
        print("\nThiếu --account. Chạy --help-setup để biết cần cung cấp gì.")
        return 1

    sessions = load_sessions()
    acc = sessions.get(args.account, {})
    result = create_deposit_order(args.account, args.amount, session=acc)
    summary = {
        "ok": result.get("ok"),
        "amount": result.get("amount"),
        "trade_no": result.get("trade_no"),
        "pay_url": result.get("pay_url"),
        "method": result.get("method"),
        "error": result.get("error"),
        "qr_image_path": result.get("qr_image_path"),
    }
    ti = result.get("transfer_info") or {}
    if ti:
        summary["transfer"] = {
            k: ti.get(k)
            for k in (
                "amount",
                "bank_name",
                "account_no",
                "account_name",
                "transfer_content",
                "qr_url",
                "expire_seconds",
                "status",
            )
        }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if result.get("qr_image_path"):
        print(f"\nQR đã lưu: {result['qr_image_path']}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
