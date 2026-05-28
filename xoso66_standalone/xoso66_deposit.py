#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XOSO66 — tạo đơn nạp (TOPAY / cổng banking) + lấy STK/nội dung CK (độc lập).

Chạy thử:
  python xoso66_deposit.py
  python xoso66_deposit.py -u tenuser -m 100000
  python xoso66_deposit.py -a acc1 -m 100000

Cần file cùng thư mục:
  xoso66_sessions.json   — cookie/session (mẫu: xoso66_sessions.example.json)
  Mã hóa: xoso66_crypto.py (đã port từ index.js — pip install pycryptodome)
"""

from __future__ import annotations

import argparse
import base64
import html as html_lib
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

from xoso66_config_util import configure_stdio_utf8

configure_stdio_utf8()

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
# QRPay (235) đã gỡ — mặc định TOPAY Ngân hàng trực tuyến (280), cấu hình trong xoso66_config.json
QRPAY_CHANNEL_ID = 235
DEFAULT_DEPOSIT_CHANNEL_ID = 280
DEFAULT_DEPOSIT_CHANNEL_NAME = "TOPAY-Ngân hàng trực tuyến"
DEFAULT_MIN_DEPOSIT_VND = 1_000_000
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


def _deposit_channel_prefs() -> dict[str, Any]:
    from xoso66_config_util import load_config

    ad = load_config().get("auto_deposit")
    if not isinstance(ad, dict):
        ad = {}
    raw_id = ad.get("deposit_channel_id")
    return {
        "channel_id": int(raw_id) if raw_id not in (None, "") else None,
        "channel_name": str(
            ad.get("deposit_channel_name") or DEFAULT_DEPOSIT_CHANNEL_NAME
        ).strip(),
        "min_amount_vnd": int(ad.get("min_deposit_vnd") or DEFAULT_MIN_DEPOSIT_VND),
        "topay_bank_bid": str(ad.get("topay_bank_bid") or "").strip(),
    }


def _iter_deposit_channels(info: dict) -> Any:
    for cat in info.get("payment_list") or []:
        for ch in cat.get("channel") or []:
            if isinstance(ch, dict):
                yield ch


def _channel_display_name(ch: dict) -> str:
    return str(ch.get("name") or ch.get("title") or ch.get("channel_name") or "")


def merchant_from_deposit_info(
    info: dict,
    *,
    channel_id: int | None = None,
    name_contains: str | None = None,
) -> tuple[int, int, str]:
    """Chọn merchant từ GET depositinfo (channel_id hoặc tên kênh)."""
    channels = list(_iter_deposit_channels(info))
    if not channels:
        raise ValueError("depositinfo không có kênh nạp")

    targets: list[dict] = []
    if channel_id is not None:
        cid = int(channel_id)
        targets = [c for c in channels if int(c.get("id") or 0) == cid]
        if not targets:
            names = [f"{c.get('id')}:{_channel_display_name(c)}" for c in channels[:8]]
            raise ValueError(
                f"Không tìm thấy channel_id={cid}. Có: {', '.join(names)}"
            )
    elif name_contains:
        key = name_contains.strip().lower()
        targets = [c for c in channels if key in _channel_display_name(c).lower()]

    if not targets:

        def _default_bank_channel(ch: dict) -> bool:
            n = _channel_display_name(ch).lower()
            if any(x in n for x in ("momo", "zalo", "usdt", "trực tuyếnqr", "mã quét")):
                return False
            return "topay" in n and "ngân hàng trực tuyến" in n

        targets = [c for c in channels if _default_bank_channel(c)]

    if not targets:
        raise ValueError(
            "Không tìm thấy kênh nạp — chỉnh deposit_channel_id / deposit_channel_name trong config"
        )

    ch = targets[0]
    merchants = ch.get("merchant") or []
    if not merchants:
        raise ValueError(
            f"Kênh {_channel_display_name(ch)} (id={ch.get('id')}) không có merchant"
        )
    m = merchants[0]
    return int(ch["id"]), int(m["id"]), str(m.get("random_remark") or "")


def _channel_exists_in_info(info: dict, channel_id: int) -> bool:
    return any(int(c.get("id") or 0) == int(channel_id) for c in _iter_deposit_channels(info))


def resolve_deposit_params(session: dict) -> tuple[int, int, str]:
    """merchant_id + random_remark — ưu tiên config, cache session nếu kênh còn tồn tại."""
    prefs = _deposit_channel_prefs()
    info = get_deposit_info(session=session)
    if not info.get("ok"):
        raise ValueError(f"depositinfo lỗi: {info.get('raw')}")

    cached_ch = int(session.get("channel_id") or 0)
    pref_ch = prefs.get("channel_id")

    if (
        session.get("merchant_id")
        and session.get("random_remark") is not None
        and cached_ch
        and cached_ch != QRPAY_CHANNEL_ID
        and _channel_exists_in_info(info, cached_ch)
        and (pref_ch is None or cached_ch == int(pref_ch))
    ):
        return (
            cached_ch,
            int(session["merchant_id"]),
            str(session.get("random_remark") or ""),
        )

    if cached_ch == QRPAY_CHANNEL_ID or (
        cached_ch and not _channel_exists_in_info(info, cached_ch)
    ):
        session.pop("channel_id", None)
        session.pop("merchant_id", None)
        session.pop("random_remark", None)

    return merchant_from_deposit_info(
        info,
        channel_id=pref_ch,
        name_contains=None if pref_ch else prefs.get("channel_name"),
    )


def qrpay_merchant_from_deposit_info(info: dict) -> tuple[int, int, str]:
    """Legacy QRPay 235 — kênh đã gỡ trên site."""
    return merchant_from_deposit_info(info, channel_id=QRPAY_CHANNEL_ID)


def resolve_qrpay_params(session: dict) -> tuple[int, int, str]:
    return resolve_deposit_params(session)


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


def apply_deposit_channel_to_session(session: dict, info: dict | None = None) -> dict:
    """Gán merchant_id / random_remark từ depositinfo (kênh theo config)."""
    if info is None:
        info = get_deposit_info(session=session)
    if not info.get("ok"):
        return session
    try:
        prefs = _deposit_channel_prefs()
        pref_ch = prefs.get("channel_id")
        ch_id, mer_id, remark = merchant_from_deposit_info(
            info,
            channel_id=pref_ch,
            name_contains=None if pref_ch else prefs.get("channel_name"),
        )
        session["channel_id"] = ch_id
        session["merchant_id"] = mer_id
        session["random_remark"] = remark
        session["bank_id"] = ""
    except ValueError:
        pass
    return session


def apply_qrpay_to_session(session: dict, info: dict | None = None) -> dict:
    return apply_deposit_channel_to_session(session, info)


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


def save_qr_image(
    transfer_info: dict,
    account_id: str,
    session: dict | None = None,
    *,
    allow_vietqr_fallback: bool = False,
) -> str | None:
    """Lưu QR từ cổng (base64 / qr_url). VietQR tự dựng chỉ khi allow_vietqr_fallback=True (QRPay)."""
    QR_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    remark = (
        transfer_info.get("transfer_content")
        or transfer_info.get("merchant_order_no")
        or transfer_info.get("trade_no")
        or "deposit"
    )
    safe = re.sub(r"[^\w\-]", "_", str(remark))[:48]
    out_path = QR_OUTPUT_DIR / f"{account_id}_{safe}.png"

    emv = transfer_info.get("qr_emv_payload")
    if emv:
        try:
            out_path.write_bytes(emv_qr_to_png_bytes(str(emv)))
            return str(out_path)
        except Exception as e:
            print(f"[QR] render EMV TOPAY: {e}", flush=True)

    uri = transfer_info.get("qr_data_uri")
    if isinstance(uri, str) and uri.startswith("data:"):
        try:
            b64 = uri.split(",", 1)[1]
            out_path.write_bytes(base64.b64decode(b64))
            return str(out_path)
        except Exception:
            pass

    url = transfer_info.get("qr_url")
    if url and not _is_decorative_payment_img(str(url)):
        src = str(transfer_info.get("source_url") or "").strip()
        hdrs = {"User-Agent": DEFAULT_UA}
        if src:
            hdrs["referer"] = src
        try:
            host = urlparse(str(url)).netloc.lower()
            if session and host and host not in urlparse(BASE_URL).netloc.lower():
                r = requests.get(str(url), headers=hdrs, timeout=25)
            else:
                r = _game_http(session or {}).get(str(url), headers=hdrs, timeout=25)
            if r.ok and r.content:
                ct = str(r.headers.get("Content-Type") or "").lower()
                if "image" in ct or str(url).lower().endswith((".png", ".jpg", ".jpeg")):
                    out_path.write_bytes(r.content)
                    return str(out_path)
        except Exception:
            pass

    if not allow_vietqr_fallback:
        return None

    # QRPay legacy — dựng VietQR (TOPAY phải dùng QR trên trang cổng)
    bank_name = transfer_info.get("bank_name") or ""
    account_no = transfer_info.get("account_no") or ""
    content = transfer_info.get("transfer_content") or ""
    amount = transfer_info.get("amount")
    code = _vietqr_bank_code(str(bank_name))
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


def transfer_info_from_deposit_data(data: dict | None) -> dict | None:
    """Parse data.info từ response depositorder (nếu site trả sẵn STK)."""
    if not isinstance(data, dict):
        return None
    info = data.get("info")
    if isinstance(info, list):
        for item in info:
            if isinstance(item, dict):
                norm = _normalize_transfer_info({"data": item})
                if norm.get("account_no"):
                    return norm
    if isinstance(info, dict):
        norm = _normalize_transfer_info({"data": info})
        if norm.get("account_no"):
            return norm
    return None


def _pay_url_trade_token(pay_url: str) -> str:
    q = urlparse(str(pay_url or "").strip())
    if q.query:
        m = re.search(r"([\w]{16,})", q.query)
        if m:
            return m.group(1)
    tail = (q.path or "").rstrip("/").split("/")[-1]
    if tail and "." not in tail and len(tail) >= 16:
        return tail
    return ""


def _should_use_qrpay_api(trade_no: str, pay_url: str | None, channel_id: int) -> bool:
    if int(channel_id or 0) == QRPAY_CHANNEL_ID:
        return True
    return "qrpay" in str(pay_url or "").lower()


def _html_element_text(html: str, element_id: str) -> str:
    """Đọc text trong #id (hỗ trợ nháy đơn/đôi, khoảng trắng)."""
    for pat in (
        rf'id=["\']{re.escape(element_id)}["\'][^>]*>\s*([^<]+)',
        rf'id=["\']{re.escape(element_id)}["\'][^>]*>([^<]+)</',
    ):
        m = re.search(pat, html, re.I | re.S)
        if m:
            return html_lib.unescape(m.group(1).strip())
    return ""


def _fetch_topay_http(
    url: str,
    topay_http: requests.Session | None = None,
    *,
    referer: str = "",
) -> str:
    """
    GET trang TOPAY và giữ cookie theo `topay_http` giữa các bước.
    Không gửi cookie xoso66-session sang domain TOPAY vì có thể làm sai luồng.
    """
    ref = str(referer or url).strip()
    headers: dict[str, str] = {
        "User-Agent": DEFAULT_UA,
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "vi,en-US;q=0.9,en;q=0.8",
    }
    if ref:
        headers["referer"] = ref
    http = topay_http or requests
    r = (
        http.get(str(url).strip(), headers=headers, timeout=25)
        if topay_http
        else requests.get(str(url).strip(), headers=headers, timeout=25)
    )
    if r.status_code != 200:
        raise RuntimeError(f"TOPAY HTTP {r.status_code}")
    return r.text


def _parse_account_from_emv(emv: str) -> str | None:
    """Số TK từ payload VietQR EMV (tag 01 trong NAPAS 38) — fallback khi HTML chưa render."""
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


def _parse_topay_paymain_fields(html: str, *, amount: int | None = None) -> dict[str, Any]:
    """STK / NDCK / EMV từ HTML paymain (một lần GET)."""
    account_no = _html_element_text(html, "account")
    emv = extract_topay_emv_payload(html)
    if not account_no and emv:
        account_no = _parse_account_from_emv(emv) or ""
    amounts_raw = _html_element_text(html, "amounts")
    amt = amount
    if amounts_raw:
        digits = re.sub(r"[^\d]", "", amounts_raw)
        if digits:
            amt = int(digits)
    ti: dict[str, Any] = {
        "amount": amt or amount,
        "bank_name": _topay_bank_name_from_html(html),
        "account_no": account_no,
        "account_name": _html_element_text(html, "userName"),
        "transfer_content": _html_element_text(html, "remake"),
        "gateway": "topay",
        "raw": {"topay_page": "paymain", "has_emv": bool(emv)},
    }
    if emv:
        ti["qr_emv_payload"] = emv
    return ti


def _abs_payment_url(page_url: str, href: str) -> str:
    href = str(href or "").strip()
    if not href:
        return ""
    if href.startswith(("data:", "http://", "https://")):
        return href
    parsed = urlparse(str(page_url or "").strip())
    if href.startswith("//"):
        return f"{parsed.scheme}:{href}"
    if href.startswith("/"):
        return f"{parsed.scheme}://{parsed.netloc}{href}"
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path or ''}"
    return f"{base.rstrip('/')}/{href.lstrip('/')}"


def _parse_qr_value(val: Any) -> tuple[str | None, str | None]:
    """
    Chuẩn hóa qrCode từ API cổng → (data_uri, url).
    Hỗ trợ data:image/..., URL http, hoặc base64 thuần.
    """
    if not isinstance(val, str):
        return None, None
    s = val.strip()
    if not s:
        return None, None
    if s.startswith("data:image"):
        return s, None
    if s.startswith("http://") or s.startswith("https://"):
        return None, s
    if len(s) > 80 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", s):
        compact = re.sub(r"\s+", "", s)
        return f"data:image/png;base64,{compact}", None
    return None, None


def extract_topay_emv_payload(html: str) -> str | None:
    """Chuỗi VietQR EMV trong script TOPAY paymain (jquery.qrcode text: toUtf8(...))."""
    if not html:
        return None
    for pat in (
        r'toUtf8\(\s*"(000201[^"]+)"\s*\)',
        r'text:\s*toUtf8\(\s*"(000201[^"]+)"\s*\)',
        r'"(00020101021\d{30,})"',
    ):
        m = re.search(pat, html, re.I | re.S)
        if m:
            return m.group(1).strip()
    return None


def _is_decorative_payment_img(url: str) -> bool:
    """Logo napas / bank / seal — không phải mã QR quét chuyển khoản."""
    low = str(url or "").lower()
    return any(
        x in low
        for x in (
            "napas",
            "bankcode",
            "globalsign",
            "pci-dss",
            "erro.png",
            "siteseal",
            "/static/public/img/",
            ".svg",
            ".webp",
        )
    )


def emv_qr_to_png_bytes(emv_payload: str) -> bytes:
    """Render PNG QR từ payload EMV (giống $('#canvas').qrcode trên TOPAY)."""
    import qrcode

    emv = str(emv_payload or "").strip()
    if not emv.startswith("000201"):
        raise ValueError("EMV payload không hợp lệ")
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=6,
        border=2,
    )
    qr.add_data(emv)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    from io import BytesIO

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def parse_payment_page_qr(html: str, page_url: str) -> dict[str, Any]:
    """Lấy QR thật từ HTML trang cổng — ưu tiên EMV TOPAY, bỏ logo napas."""
    out: dict[str, Any] = {}
    if not html:
        return out

    emv = extract_topay_emv_payload(html)
    if emv:
        out["qr_emv_payload"] = emv
        return out

    m = re.search(
        r"(data:image/(?:png|jpeg|jpg);base64,[A-Za-z0-9+/=]+)",
        html,
        re.I,
    )
    if m:
        out["qr_data_uri"] = m.group(1)
        return out

    img_patterns = (
        r'<img[^>]+(?:id|class)=["\'][^"\']*qr[^"\']*["\'][^>]+src=["\']([^"\']+)["\']',
        r'<img[^>]+src=["\']([^"\']+)["\'][^>]+(?:id|class)=["\'][^"\']*qr[^"\']*["\']',
        r'<img[^>]+src=["\']([^"\']*(?:showqr|showQr|qrcode\.do)[^"\']*)["\']',
    )
    for pat in img_patterns:
        for im in re.finditer(pat, html, re.I):
            src = _abs_payment_url(page_url, im.group(1))
            if not src or _is_decorative_payment_img(src):
                continue
            if src.startswith("data:image"):
                out["qr_data_uri"] = src
                return out
            out["qr_url"] = src
            return out

    return out


def transfer_info_has_qr(ti: dict[str, Any] | None) -> bool:
    if not isinstance(ti, dict):
        return False
    if ti.get("qr_emv_payload") or ti.get("qr_data_uri") or ti.get("qr_url"):
        return True
    return bool(str(ti.get("qr_base64") or "").strip())


def _vietqr_bank_code(bank_name: str) -> str:
    """Map tên ngân hàng site → mã VietQR (fallback cuối, không dùng cho TOPAY)."""
    s = str(bank_name or "").upper()
    rules = (
        ("ACB", "ACB"),
        ("VIETCOMBANK", "VCB"),
        ("VCB", "VCB"),
        ("TECHCOMBANK", "TCB"),
        ("TCB", "TCB"),
        ("VPBANK", "VPB"),
        ("VPB", "VPB"),
        ("BIDV", "BIDV"),
        ("MBBANK", "MB"),
        ("MB BANK", "MB"),
        ("TPBANK", "TPB"),
        ("SACOMBANK", "STB"),
        ("AGRIBANK", "VBA"),
    )
    for needle, code in rules:
        if needle in s:
            return code
    token = re.split(r"[\s\-–—|]", s.strip(), maxsplit=1)[0]
    return token[:8] if token else ""


def qr_image_to_data_uri(
    *,
    qr_path: str = "",
    transfer_info: dict[str, Any] | None = None,
    http_headers: dict[str, str] | None = None,
) -> str:
    """Ảnh QR gửi bên thứ 3 — chỉ từ file / transfer_info, không tải pay_url HTML."""
    ti = transfer_info if isinstance(transfer_info, dict) else {}

    uri = ti.get("qr_data_uri")
    if isinstance(uri, str) and uri.startswith("data:image"):
        return uri

    emv = ti.get("qr_emv_payload")
    if emv:
        try:
            raw = emv_qr_to_png_bytes(str(emv))
            return f"data:image/png;base64,{base64.b64encode(raw).decode()}"
        except Exception:
            pass

    raw_b64 = ti.get("qr_base64")
    if isinstance(raw_b64, str) and len(raw_b64.strip()) > 80:
        s = raw_b64.strip()
        if s.startswith("data:image"):
            return s
        return f"data:image/png;base64,{s}"

    path = str(qr_path or ti.get("qr_image_path") or "").strip()
    if path and os.path.isfile(path):
        try:
            raw = Path(path).read_bytes()
            return f"data:image/png;base64,{base64.b64encode(raw).decode()}"
        except Exception:
            pass

    url = str(ti.get("qr_url") or "").strip()
    if url.startswith("http"):
        hdrs = dict(http_headers or {})
        hdrs.setdefault("User-Agent", DEFAULT_UA)
        try:
            r = requests.get(url, headers=hdrs, timeout=25)
            if r.ok and r.content and "image" in str(
                r.headers.get("Content-Type") or ""
            ).lower():
                return f"data:image/png;base64,{base64.b64encode(r.content).decode()}"
        except Exception:
            pass
    return ""


def _topay_bank_name_from_html(html: str) -> str:
    for pat in (
        r"<th>\s*Ng[^<]*</th>\s*<td[^>]*>([^<]+)</td>",
        r"Ngân hàng</th>\s*<td>([^<]+)</td>",
    ):
        m = re.search(pat, html, re.I | re.S)
        if m:
            return html_lib.unescape(m.group(1).strip())
    return ""


def _topay_order_context(html: str, pay_url: str) -> str:
    """Mã đơn TOPAY: hidden order_code hoặc oid/o_code trên pay_url."""
    for pat in (
        r'id=["\']order_code["\'][^>]*value=["\']([^"\']+)["\']',
        r'name=["\']order_code["\'][^>]*value=["\']([^"\']+)["\']',
        r'id=["\']o_code["\'][^>]*value=["\']([^"\']+)["\']',
        r'name=["\']o_code["\'][^>]*value=["\']([^"\']+)["\']',
    ):
        m = re.search(pat, html, re.I)
        if m:
            return html_lib.unescape(m.group(1).strip())
    # Fallback: o_code/orderCode trong JS (khi không có input hidden như regex đầu).
    for pat in (
        r"o_code['\"]?\s*[:=]\s*['\"]([^'\"]+)['\"]",
        r"orderCode['\"]?\s*[:=]\s*['\"]([^'\"]+)['\"]",
        r"order_code['\"]?\s*[:=]\s*['\"]([^'\"]+)['\"]",
    ):
        m = re.search(pat, html, re.I | re.S)
        if m:
            return html_lib.unescape(m.group(1).strip())
    q = parse_qs(urlparse(str(pay_url or "").strip()).query)
    for key in ("o_code", "order_code", "oid", "orderCode"):
        vals = q.get(key)
        if vals and str(vals[0]).strip():
            return str(vals[0]).strip()
    return ""


def _topay_collect_bank_bids(html: str) -> list[str]:
    """Danh sách bid ngân hàng trên trang chọn NH (data-code / link proceed)."""
    bids: list[str] = []
    for pat in (
        r'data-code=["\']([^"\']+)["\']',
        r'proceed_deposit\.do\?bid=([^&"\']+)',
        r'["\']bid["\']\s*:\s*["\']([^"\']+)["\']',
    ):
        bids.extend(re.findall(pat, html, re.I))
    out: list[str] = []
    for b in bids:
        b = str(b).strip()
        if b and b not in out:
            out.append(b)
    return out


def _topay_bid_try_order(bids: list[str], configured: str) -> list[str]:
    order: list[str] = []
    cfg = str(configured or "").strip()
    if cfg:
        order.append(cfg)
    for b in bids:
        if b not in order:
            order.append(b)
    return order


def fetch_topay_paymain_info(
    pay_url: str,
    *,
    amount: int | None = None,
    session: dict | None = None,
) -> dict:
    """
    TOPAY paymain: STK/NDCK + EMV trong script (#canvas qrcode).
    Trang có thể chưa render ngay sau tạo đơn — retry vài giây.
    """
    pay_url = str(pay_url or "").strip()
    topay_http = requests.Session()
    last_err = ""
    for attempt in range(1, 4):
        html = _fetch_topay_http(pay_url, topay_http, referer=pay_url)
        ti = _parse_topay_paymain_fields(html, amount=amount)
        ti["source_url"] = pay_url
        has_emv = bool(ti.get("qr_emv_payload"))
        has_acct = bool(str(ti.get("account_no") or "").strip())
        if has_emv or has_acct:
            if attempt > 1:
                print(
                    f"[TOPAY] paymain OK sau lần {attempt} "
                    f"(STK={'có' if has_acct else 'EMV'})",
                    flush=True,
                )
            elif has_emv and not has_acct:
                print(
                    "[TOPAY] paymain: STK lấy từ EMV (HTML chưa có #account)",
                    flush=True,
                )
            return ti
        last_err = "chưa có STK/EMV trên paymain"
        if attempt < 3:
            time.sleep(1.2)
    raise RuntimeError(last_err)


def fetch_topay_transfer_info(
    pay_url: str,
    *,
    bank_bid: str | None = None,
    amount: int | None = None,
    session: dict | None = None,
) -> dict:
    """
    TOPAY: trang cổng thường bắt chọn 1 ngân hàng → proceed_deposit.do mới có QR EMV.
    Giống thao tác trên UI bên thứ 3 (chọn NH rồi mới quét mã).
    """
    pay_url = str(pay_url or "").strip()
    parsed = urlparse(pay_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    cfg_bid = str(bank_bid or "").strip()

    topay_http = requests.Session()
    html = _fetch_topay_http(pay_url, topay_http, referer=pay_url)
    ti_peek = _parse_topay_paymain_fields(html, amount=amount)
    bank_bids = _topay_collect_bank_bids(html)
    order_code = _topay_order_context(html, pay_url)

    # Paymain đã render sẵn QR + STK, không có bước chọn NH trên HTML
    if (
        transfer_info_has_qr(ti_peek)
        and str(ti_peek.get("account_no") or "").strip()
        and not bank_bids
    ):
        ti_peek["source_url"] = pay_url
        print("[TOPAY] paymain đủ QR (không cần chọn bank)", flush=True)
        return ti_peek

    if not order_code:
        raise RuntimeError("TOPAY: không có order_code / oid trên link nạp")

    # Link paymain?oid=… thường chưa có list NH — mở trang index cùng oid để lấy data-code
    if not bank_bids and re.search(r"paymain", pay_url, re.I):
        list_url = re.sub(r"paymain", "index", pay_url, flags=re.I)
        if list_url != pay_url:
            try:
                html_list = _fetch_topay_http(
                    list_url, topay_http, referer=pay_url
                )
                bank_bids = _topay_collect_bank_bids(html_list)
                order_code = order_code or _topay_order_context(html_list, list_url)
            except Exception as e:
                print(f"[TOPAY] không mở được trang chọn NH: {e}", flush=True)

    try_bids = _topay_bid_try_order(bank_bids, cfg_bid)
    if not try_bids:
        raise RuntimeError(
            "TOPAY: không thấy danh sách bank (data-code) — "
            "cấu hình auto_deposit.topay_bank_bid (bid lấy khi bấm 1 NH trên trang)"
        )

    if len(try_bids) > 1 or bank_bids:
        print(
            f"[TOPAY] chọn ngân hàng (bid) — thử {len(try_bids)} mã: "
            f"{', '.join(try_bids[:4])}{'…' if len(try_bids) > 4 else ''}",
            flush=True,
        )

    last_err = ""
    for bid in try_bids:
        proceed_url = f"{base}/index/proceed_deposit.do?bid={bid}&o_code={order_code}"
        try:
            body = _fetch_topay_http(proceed_url, topay_http, referer=pay_url)
            ti = _parse_topay_paymain_fields(body, amount=amount)
            if not transfer_info_has_qr(ti):
                extra = parse_payment_page_qr(body, proceed_url)
                ti.update({k: v for k, v in extra.items() if v})
            if transfer_info_has_qr(ti):
                ti["source_url"] = proceed_url
                ti["gateway"] = "topay"
                ti["raw"] = {
                    "topay_bid": bid,
                    "order_code": order_code[:24],
                    "topay_page": "proceed",
                }
                if not str(ti.get("account_no") or "").strip() and ti.get("qr_emv_payload"):
                    ti["account_no"] = _parse_account_from_emv(
                        str(ti["qr_emv_payload"])
                    ) or ti.get("account_no")
                print(
                    f"[TOPAY] bid={bid} → có QR EMV"
                    + (
                        f" | STK {ti.get('account_no')} | NDCK {ti.get('transfer_content')}"
                        if ti.get("account_no")
                        else ""
                    ),
                    flush=True,
                )
                return ti
            last_err = f"bid={bid}: chưa có EMV"
        except Exception as e:
            last_err = f"bid={bid}: {e}"

    if "paymain" in pay_url.lower():
        try:
            ti = fetch_topay_paymain_info(pay_url, amount=amount, session=session)
            if transfer_info_has_qr(ti):
                return ti
        except Exception:
            pass

    raise RuntimeError(
        f"TOPAY: đã thử chọn bank nhưng chưa lấy được QR — {last_err}"
    )


def fetch_transfer_info_from_pay_url(
    pay_url: str,
    *,
    amount: int | None = None,
    session: dict | None = None,
) -> dict:
    """
    Lấy STK/NDCK từ trang cổng thanh toán (TOPAY / TIMEPAY-style API).
    """
    pay_url = str(pay_url or "").strip()
    if not pay_url:
        raise ValueError("pay_url trống")
    if "topay" in pay_url.lower():
        prefs = _deposit_channel_prefs()
        return fetch_topay_transfer_info(
            pay_url,
            bank_bid=str(prefs.get("topay_bank_bid") or "") or None,
            amount=amount,
            session=session,
        )
    token = _pay_url_trade_token(pay_url)
    if not token:
        raise ValueError(f"Không parse token từ pay_url: {pay_url[:80]}")
    parsed = urlparse(pay_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    api = f"{origin}/user/tradepay/{token}"
    headers = {
        "accept": "application/json, text/plain, */*",
        "user-agent": DEFAULT_UA,
        "referer": pay_url,
    }
    r = requests.get(api, headers=headers, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"Pay gateway HTTP {r.status_code}: {r.text[:200]}")
    try:
        js = r.json()
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Pay gateway không phải JSON: {r.text[:200]}") from e
    data = js.get("data") if isinstance(js.get("data"), dict) else {}
    ok = js.get("success") is True or str(js.get("code")) in ("200", "1")
    if not ok and not data.get("bankAccount"):
        raise RuntimeError(js.get("message") or f"Pay gateway code={js.get('code')}")
    qr_uri, qr_url = _parse_qr_value(data.get("qrCode"))
    ti = {
        "amount": data.get("amount") or amount,
        "bank_name": data.get("bankName") or data.get("bankCode"),
        "account_no": data.get("bankAccount") or data.get("accountNo"),
        "account_name": data.get("payName") or data.get("accountName"),
        "transfer_content": data.get("descCode") or data.get("remark"),
        "qr_data_uri": qr_uri,
        "qr_url": qr_url or data.get("qrUrl") or data.get("qr_url"),
        "raw": js,
        "source_url": api,
    }
    if not ti.get("account_no"):
        raise RuntimeError("API cổng không trả số tài khoản (đơn hết hạn?)")
    return ti


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
    Tạo đơn nạp XOSO66 (TOPAY / cổng banking) + lấy STK/nội dung CK.
    """
    prefs = _deposit_channel_prefs()
    amount = max(int(amount), int(prefs["min_amount_vnd"]))

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
        apply_deposit_channel_to_session(session)
    get_deposit_info(session=session)
    session.pop("aes_session_key", None)
    ch_id, mer_id, remark = resolve_deposit_params(session)
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
        "channel_id": ch_id,
        "pay_url": pay_url,
        "trade_no": trade_no,
        "order_no": parsed.get("order_no"),
        "action": parsed.get("action"),
        "deposit_raw": parsed.get("raw"),
        "method": method,
    }

    ti: dict | None = None
    raw = parsed.get("raw")
    if isinstance(raw, dict) and isinstance(raw.get("data"), dict):
        ti = transfer_info_from_deposit_data(raw["data"])

    if trade_no and _should_use_qrpay_api(trade_no, pay_url, ch_id):
        try:
            ti = fetch_qrpay_transfer_info(trade_no, session=session)
        except Exception as e:
            out["transfer_info_error"] = str(e)
    elif pay_url:
        try:
            ti = ti or fetch_transfer_info_from_pay_url(
                pay_url, amount=amount, session=session
            )
        except Exception as e:
            out["transfer_info_error"] = str(e)

    if ti:
        out["transfer_info"] = ti
        try:
            use_vietqr_fb = bool(
                trade_no and _should_use_qrpay_api(trade_no, pay_url, ch_id)
            )
            qr_path = save_qr_image(
                ti,
                account_id,
                session=session,
                allow_vietqr_fallback=use_vietqr_fb,
            )
            if qr_path:
                out["qr_image_path"] = qr_path
            elif not transfer_info_has_qr(ti):
                print(
                    f"[NẠP] {account_id}: không lưu được file QR từ cổng",
                    flush=True,
                )
        except Exception:
            pass

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
    info: dict[str, Any] = {
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
        "qr_data_uri": None,
        "merchant_order_no": d.get("merchatOrderNo") or d.get("merchantOrderNo") or d.get("systemOrderNo"),
        "expire_seconds": d.get("expireTime"),
        "status": d.get("statusStr"),
        "raw": js,
    }
    qr_uri, qr_url = _parse_qr_value(d.get("qrCode"))
    if qr_uri:
        info["qr_data_uri"] = qr_uri
    if qr_url:
        info["qr_url"] = qr_url
    return info


# ── CLI ──────────────────────────────────────────────────────────────────────

def _parse_amount_vnd(raw: str) -> int:
    s = str(raw or "").strip().replace(",", "").replace(".", "").replace(" ", "")
    if not s:
        raise ValueError("số tiền trống")
    return int(s)


def resolve_account_id(account_id: str = "", username: str = "") -> str:
    """account id hoặc username → id trong DB / sessions."""
    for key in (str(username or "").strip(), str(account_id or "").strip()):
        if not key:
            continue
        try:
            from xoso66_accounts_db import get_account, get_account_by_username

            row = get_account(key) or get_account_by_username(key)
            if row:
                return str(row["id"])
        except Exception:
            pass
        sessions = load_sessions()
        if key in sessions:
            return key
        ku = key.lower()
        for aid, acc in sessions.items():
            if str(acc.get("username") or "").strip().lower() == ku:
                return aid
    label = str(username or account_id or "").strip() or "?"
    raise SystemExit(f"Không tìm thấy account: {label}")


def _prompt_username(args: argparse.Namespace) -> None:
    if args.account or args.username:
        return
    try:
        args.username = input("Username: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(1) from None
    if not args.username:
        raise SystemExit("Username không được để trống.")


def _prompt_amount(args: argparse.Namespace) -> None:
    if args.amount is not None:
        return
    try:
        raw = input("Số tiền (VND): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(1) from None
    if not raw:
        raw = "100000"
    try:
        args.amount = _parse_amount_vnd(raw)
    except ValueError as e:
        raise SystemExit(f"Số tiền không hợp lệ: {e}") from e


def print_deposit_result(result: dict) -> None:
    """In kết quả lệnh nạp (text + JSON)."""
    summary = {
        "ok": result.get("ok"),
        "account_id": result.get("account_id"),
        "amount": result.get("amount"),
        "trade_no": result.get("trade_no"),
        "order_no": result.get("order_no"),
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
    print("\n═══ KẾT QUẢ LỆNH NẠP ═══", flush=True)
    if result.get("ok"):
        print(f"  Trạng thái : OK", flush=True)
        print(f"  Số tiền    : {int(result.get('amount') or 0):,} VND", flush=True)
        if result.get("trade_no"):
            print(f"  Mã đơn     : {result.get('trade_no')}", flush=True)
        if result.get("order_no"):
            print(f"  Order no   : {result.get('order_no')}", flush=True)
        if ti:
            print(f"  Ngân hàng  : {ti.get('bank_name') or '-'}", flush=True)
            print(f"  STK        : {ti.get('account_no') or '-'}", flush=True)
            print(f"  Chủ TK     : {ti.get('account_name') or '-'}", flush=True)
            print(f"  Nội dung CK: {ti.get('transfer_content') or '-'}", flush=True)
            if ti.get("expire_seconds"):
                print(f"  Hết hạn    : {ti.get('expire_seconds')}s", flush=True)
        if result.get("pay_url"):
            print(f"  Pay URL    : {result.get('pay_url')}", flush=True)
        if result.get("qr_image_path"):
            print(f"  QR file    : {result.get('qr_image_path')}", flush=True)
    else:
        print(f"  Trạng thái : LỖI", flush=True)
        print(f"  Lý do      : {result.get('error') or 'không rõ'}", flush=True)
    print("\n--- JSON ---", flush=True)
    print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)


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

3) Kênh nạp: TOPAY Ngân hàng trực tuyến (channel 280) — min 1M, chỉnh trong xoso66_config.json

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
    parser.add_argument(
        "--account",
        "-a",
        help="account id hoặc username (DB / xoso66_sessions.json)",
    )
    parser.add_argument("-u", "--username", help="username trong DB / sessions")
    parser.add_argument("--amount", "-m", type=int, default=None, help="số tiền VND")
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
        _prompt_username(args)
        aid = resolve_account_id(args.account or "", args.username or "")
        sessions = load_sessions()
        result = get_deposit_info(aid, sessions[aid])
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    if args.list_orders:
        _prompt_username(args)
        aid = resolve_account_id(args.account or "", args.username or "")
        result = list_payment_orders(aid, status=args.status)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    if args.login:
        _prompt_username(args)
        aid = resolve_account_id(args.account or "", args.username or "")
        from xoso66_session import ensure_session

        ensure_session(aid, force_login=True)
        print(f"OK: session renewed → {SESSIONS_FILE}")
        return 0

    _prompt_username(args)
    _prompt_amount(args)
    aid = resolve_account_id(args.account or "", args.username or "")
    sessions = load_sessions()
    acc = sessions.get(aid, {})
    result = create_deposit_order(aid, args.amount, session=acc)
    print_deposit_result(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
