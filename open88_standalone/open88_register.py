# -*- coding: utf-8 -*-
"""
Thử đăng ký OPEN88 (www.open8808.com) — PUT /wps/member/register + RSA/DES.

Cần: Node.js, requests.
  pip install requests

Lưu ý: site thường bắt GeeTest + captcha/SMS — script này chỉ thử payload mã hóa đúng format.
"""

from __future__ import annotations

import json
import random
import string
import subprocess
import sys
import time
import uuid
from pathlib import Path

import requests

BASE = "https://www.open8808.com"
MERCHANT = "op88vndkf2"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
BRIDGE = Path(__file__).resolve().parent / "open88_encrypt_bridge.js"


def _rnd_username() -> str:
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"quang{suffix}"


def _rnd_password() -> str:
    return "Abc" + "".join(random.choices(string.digits, k=6))


def _rnd_phone() -> str:
    return "09" + "".join(random.choices(string.digits, k=8))


def fetch_rsa_key(session: requests.Session) -> str:
    url = f"{BASE}/wps/session/key/rsa?t={int(time.time() * 1000)}"
    r = session.get(url, timeout=30)
    r.raise_for_status()
    key = (r.text or "").strip()
    if len(key) < 64:
        raise RuntimeError(f"RSA key invalid: {key[:80]!r}")
    return key


def encrypt_payload(rsa_modulus: str, payload: dict) -> dict:
    plain = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    proc = subprocess.run(
        ["node", str(BRIDGE), rsa_modulus, plain],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or "encrypt failed")
    return json.loads(proc.stdout.strip())


def default_headers(*, encryption_rsa: str = "") -> dict[str, str]:
    """Giống axios interceptor trên site (Merchant, Device, Language + Encryption)."""
    h = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": BASE,
        "Referer": f"{BASE}/register",
        "Merchant": MERCHANT,
        "Device": "web",
        "Language": "VI",
    }
    if encryption_rsa:
        h["Encryption"] = encryption_rsa
    return h


def get_captcha(session: requests.Session) -> dict:
    """GET /wps/captcha."""
    r = session.get(f"{BASE}/wps/captcha", headers=default_headers(), timeout=30)
    try:
        return r.json()
    except Exception:
        return {"ok": False, "status": r.status_code, "text": r.text[:500]}


def try_register(session: requests.Session, *, data: dict) -> dict:
    rsa = fetch_rsa_key(session)
    enc = encrypt_payload(rsa, data)
    h = default_headers(encryption_rsa=enc["RSA"])
    r = session.put(
        f"{BASE}/wps/member/register",
        headers=h,
        json={"value": enc["DES"]},
        timeout=60,
    )
    out: dict = {"http_status": r.status_code, "headers": dict(r.headers)}
    try:
        out["json"] = r.json()
    except Exception:
        out["text"] = (r.text or "")[:2000]
    return out


def main() -> int:
    session = requests.Session()
    session.get(f"{BASE}/", headers=default_headers(), timeout=30)

    username = _rnd_username()
    password = _rnd_password()
    phone = _rnd_phone()
    payee = "NGUYEN VAN A"

    data = {
        "username": username,
        "password": password,
        "confirmPassword": password,
        "payeeName": payee,
        "mobileNum": phone,
        "captcha": "",
        "verificationCode": "",
        "affiliateCode": "",
        "referralCode": "",
        "paymentPassword": password,
        "email": "",
        "registerMethod": "WEB",
        "registerUrl": f"{BASE}/register",
        "domain": "www.open8808.com",
        "login": True,
        "loginDeviceId": str(uuid.uuid4()),
    }

    cap = get_captcha(session)
    print("=== captcha ===", "ok" if cap.get("success") else cap)

    print("\n=== register attempt ===")
    print(f"username={username} password={password} phone={phone}")
    result = try_register(session, data=data)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    j = result.get("json") or {}
    if j.get("success"):
        print("\nOK — đăng ký có vẻ thành công (kiểm tra response data).")
        return 0
    print(f"\nChưa thành công: {j.get('errorCode') or j.get('message') or result}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
