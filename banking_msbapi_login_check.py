# -*- coding: utf-8 -*-
"""Gọi Banking HMAC API trước nạp/rút — login-check MSBAPI (XMSB*).

LC79 / xoso66:
  - Nạp: 1 lần ở create-deposit (trước lấy QR), không check lại khi gửi banking.
  - Rút: 1 lần ở withdraw ngay trước khi gọi API game.
Bắt buộc partnerId + apiKey + apiSecret (HMAC).
"""
from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from typing import Any
from urllib.parse import quote, urlparse

import requests

_LOG = "[MSBAPI-LOGIN-CHECK]"


def is_msbapi_device(device: str) -> bool:
    return str(device or "").strip().upper().startswith("XMSB")


def banking_base_from_third_party_url(third_party_url: str) -> str:
    """Base Flask HMAC (:8888). URL :3010 → đổi sang :8888."""
    u = str(third_party_url or "").strip()
    if not u:
        return "http://127.0.0.1:8888"
    if "/api/" in u:
        base = u.rsplit("/api/", 1)[0].rstrip("/")
    else:
        base = u.rstrip("/")
    if base.endswith(":3010"):
        base = base[:-5] + ":8888"
    return base or "http://127.0.0.1:8888"


def hmac_partner_headers(
    method: str,
    url_or_path: str,
    body: bytes,
    *,
    partner_id: str,
    api_key: str,
    api_secret: str,
) -> dict[str, str]:
    """Header HMAC cổng Banking partner."""
    raw = url_or_path or ""
    if "://" in raw:
        path = urlparse(raw).path or "/"
    else:
        path = raw
    if "?" in path:
        path = path.split("?", 1)[0]
    if not path.startswith("/"):
        path = "/" + path
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex
    body_hash = hashlib.sha256(body or b"").hexdigest()
    canonical = f"{method.upper()}\n{path}\n{ts}\n{nonce}\n{body_hash}"
    sig = hmac.new(
        api_secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Partner-Id": partner_id,
        "X-Api-Key": api_key,
        "X-Timestamp": ts,
        "X-Nonce": nonce,
        "X-Signature": sig,
    }


def require_partner_hmac(partner_id: str, api_key: str, api_secret: str) -> str:
    """Trả chuỗi lỗi nếu thiếu HMAC; rỗng nếu đủ."""
    if not str(partner_id or "").strip():
        return "Thiếu partnerId"
    if not str(api_key or "").strip() or not str(api_secret or "").strip():
        return "Thiếu partner_api_key / partner_api_secret (bắt buộc HMAC)"
    return ""


def check_device_login(
    device: str,
    *,
    banking_base_url: str,
    partner_id: str,
    api_key: str,
    api_secret: str,
    timeout: float = 45.0,
) -> dict[str, Any]:
    """
    Thử login MSBAPI theo tên device (HMAC bắt buộc).

    Trả:
      ok=True, loginOk=True/False — gọi API thành công
      ok=False — thiếu HMAC / lỗi mạng / HTTP
      skipped=True — không phải XMSB (không check)
    """
    dev = str(device or "").strip()
    if not is_msbapi_device(dev):
        return {
            "ok": True,
            "skipped": True,
            "loginOk": True,
            "device": dev,
            "reason": "not_msbapi_device",
        }

    miss = require_partner_hmac(partner_id, api_key, api_secret)
    if miss:
        print(f"{_LOG} {dev}: {miss}", flush=True)
        return {"ok": False, "loginOk": False, "device": dev, "error": miss}

    base = banking_base_from_third_party_url(banking_base_url)
    path = f"/api/msbapi/devices/{quote(dev, safe='')}/login-check"
    url = f"{base}{path}"
    body = b""
    headers = hmac_partner_headers(
        "GET",
        path,
        body,
        partner_id=str(partner_id).strip(),
        api_key=str(api_key).strip(),
        api_secret=str(api_secret).strip(),
    )
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        data = resp.json() if resp.content else {}
        if not isinstance(data, dict):
            data = {}
        if resp.status_code >= 400:
            err = str(data.get("error") or resp.text[:200] or f"HTTP {resp.status_code}")
            print(f"{_LOG} {dev}: HTTP {resp.status_code} — {err}", flush=True)
            return {
                "ok": False,
                "loginOk": False,
                "device": dev,
                "error": err,
                "status_code": resp.status_code,
            }
        login_ok = bool(data.get("loginOk"))
        print(f"{_LOG} {dev}: loginOk={login_ok}", flush=True)
        return {
            "ok": True,
            "loginOk": login_ok,
            "device": str(data.get("device") or dev),
            "raw": data,
        }
    except Exception as e:
        print(f"{_LOG} {dev}: lỗi gọi API — {e}", flush=True)
        return {"ok": False, "loginOk": False, "device": dev, "error": str(e)}


def assert_device_login_ok(
    device: str,
    *,
    banking_base_url: str,
    partner_id: str,
    api_key: str,
    api_secret: str,
    timeout: float = 45.0,
    label: str = "",
) -> dict[str, Any]:
    """Chặn nạp/rút nếu thiếu HMAC hoặc XMSB không login được."""
    tag = f" [{label}]" if label else ""
    rep = check_device_login(
        device,
        banking_base_url=banking_base_url,
        partner_id=partner_id,
        api_key=api_key,
        api_secret=api_secret,
        timeout=timeout,
    )
    if rep.get("skipped"):
        return {"ok": True, "skipped": True, "device": str(device or ""), "check": rep}
    if not rep.get("ok"):
        err = str(rep.get("error") or "không gọi được login-check")
        print(f"{_LOG}{tag} CHẶN — {device}: {err}", flush=True)
        return {
            "ok": False,
            "error": f"Không kiểm tra được login MSBAPI {device}: {err}",
            "device": device,
            "check": rep,
        }
    if not rep.get("loginOk"):
        print(f"{_LOG}{tag} CHẶN — {device} không login được", flush=True)
        return {
            "ok": False,
            "error": f"Device {device} không login được MSBAPI — bỏ qua nạp/rút",
            "device": device,
            "check": rep,
        }
    return {"ok": True, "device": device, "check": rep}


def should_lock_after_login_check(gate: dict[str, Any]) -> bool:
    """True khi API login-check trả lời rõ: không login được (không phải lỗi mạng/HMAC)."""
    if gate.get("ok") or gate.get("skipped"):
        return False
    check = gate.get("check") if isinstance(gate.get("check"), dict) else {}
    return bool(check.get("ok") and check.get("loginOk") is False)


def lock_lc79_account(username: str, *, reason: str = "MSBAPI login fail") -> bool:
    user = str(username or "").strip()
    if not user:
        return False
    try:
        from status_utils import update_status

        ok = bool(update_status(user, "Khoá"))
        print(f"{_LOG} LC79 {user} → Khoá ({reason}) ok={ok}", flush=True)
        return ok
    except Exception as e:
        print(f"{_LOG} LC79 khoá {user} lỗi: {e}", flush=True)
        return False


def lock_xoso66_account(
    account_id: str = "",
    username: str = "",
    *,
    reason: str = "MSBAPI login fail",
) -> bool:
    try:
        from xoso66_accounts_db import (
            STATUS_KHOA,
            get_account_by_username,
            set_account_status,
        )

        aid = str(account_id or "").strip()
        user = str(username or "").strip()
        if not aid and user:
            row = get_account_by_username(user)
            aid = str((row or {}).get("id") or "").strip()
        if not aid:
            print(f"{_LOG} xoso66 khoá: thiếu account_id ({user})", flush=True)
            return False
        return bool(set_account_status(aid, STATUS_KHOA, reason=reason))
    except Exception as e:
        print(f"{_LOG} xoso66 khoá lỗi: {e}", flush=True)
        return False


def resolve_lc79_device(username: str, *, cms_api_base: str = "http://127.0.0.1:3000") -> str:
    user = str(username or "").strip()
    if not user:
        return ""
    try:
        r = requests.get(f"{cms_api_base.rstrip('/')}/api/accounts/{user}", timeout=8)
        if r.status_code == 200:
            row = r.json() if r.content else {}
            if isinstance(row, dict):
                return str(row.get("device") or "").strip()
    except Exception as e:
        print(f"{_LOG} CMS device {user}: {e}", flush=True)
    return ""


def resolve_xoso66_device(account_id: str = "", username: str = "") -> str:
    try:
        from xoso66_accounts_db import get_account, get_account_by_username

        aid = str(account_id or "").strip()
        user = str(username or "").strip()
        row = get_account(aid) if aid else None
        if not row and user:
            row = get_account_by_username(user)
        if isinstance(row, dict):
            return str(row.get("device") or "").strip()
    except Exception as e:
        print(f"{_LOG} xoso66 device: {e}", flush=True)
    return ""
