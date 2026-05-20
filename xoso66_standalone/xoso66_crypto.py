# -*- coding: utf-8 -*-
"""
Port tu index.93f7a11b.js (module b775, ~dong 186533-186647).
AES-128-ECB + RSA (JSEncrypt) cho cek-k / body depositorder.
"""
from __future__ import annotations

import base64
import json
import secrets
import textwrap
from typing import Any

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad


def random_aes_key() -> str:
    """16 ky tu hex — giong bien `c` trong JS (moi phien/trang)."""
    return "".join(secrets.choice("0123456789ABCDEF") for _ in range(16))


def _pair_scramble(s: str) -> str:
    n = len(s)
    out: list[str] = []
    i = 0
    r = 2
    while i < n:
        chunk = s[i : i + r]
        out.append(chunk[::-1])
        i += r
        r += 1
        if r > 8:
            r = 2
    return "".join(out)


def decode_cek_p(cek_p: str) -> str:
    """Ham h(t) — tu header cek-p ra PEM public key RSA (JSEncrypt)."""
    raw = base64.b64decode(cek_p)
    s = raw.decode("latin-1")
    b64 = _pair_scramble(s).replace("\n", "").strip()
    if "BEGIN PUBLIC KEY" in b64:
        return b64
    wrapped = "\n".join(textwrap.wrap(b64, 64))
    return f"-----BEGIN PUBLIC KEY-----\n{wrapped}\n-----END PUBLIC KEY-----"


def make_cek_k(aes_key: str, cek_p: str) -> str:
    """Header cek-k."""
    if not cek_p:
        raise ValueError("Thieu cek_p — goi API truoc (login/init) de lay header cek-p")
    pem = decode_cek_p(cek_p)
    rsa_key = RSA.import_key(pem)
    cipher = PKCS1_v1_5.new(rsa_key)
    enc = cipher.encrypt(aes_key.encode("utf-8"))
    if not enc:
        raise ValueError("RSA encrypt failed — kiem tra cek_p")
    enc_str = base64.b64encode(enc).decode("ascii")
    return base64.b64encode(_pair_scramble(enc_str).encode("latin-1")).decode("ascii")


def encrypt_payload(plain: dict | str, aes_key: str, cek_p: str) -> tuple[str, str]:
    """Tra (body_base64, cek_k)."""
    if isinstance(plain, dict):
        text = json.dumps(plain, separators=(",", ":"), ensure_ascii=False)
    else:
        text = plain
    key = aes_key.encode("utf-8")
    cipher = AES.new(key, AES.MODE_ECB)
    body = base64.b64encode(cipher.encrypt(pad(text.encode("utf-8"), 16))).decode("ascii")
    cek_k = make_cek_k(aes_key, cek_p)
    return body, cek_k


def decrypt_response(cipher_text: str, aes_key: str) -> Any:
    """Giai response khi header cek-s: 1."""
    s = cipher_text.strip()
    if s.startswith('"') and s.endswith('"'):
        s = s[1:-1]
    key = aes_key.encode("utf-8")
    raw = base64.b64decode(s)
    cipher = AES.new(key, AES.MODE_ECB)
    plain = unpad(cipher.decrypt(raw), 16).decode("utf-8")
    try:
        return json.loads(plain)
    except json.JSONDecodeError:
        return plain
