# -*- coding: utf-8 -*-
"""
Mã hóa/giải mã API BEN Bet (api.bencloud.io) — port từ bundle index*.js (AES-CBC).
"""
from __future__ import annotations

import base64
import json
from typing import Any

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

# a.enc.Utf8.parse(...) trong JS frontend benhome1.vip
_AES_KEY = b"CaBqr$1SHhyTKrjETRuihhgKKFtyCgvD"
_AES_IV = b"K4aU7hh2grnU%Q1b"


def encrypt_body(payload: dict[str, Any]) -> str:
    """JSON → base64 ciphertext (field `data` gửi lên server)."""
    plain = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    cipher = AES.new(_AES_KEY, AES.MODE_CBC, _AES_IV)
    return base64.b64encode(cipher.encrypt(pad(plain.encode("utf-8"), 16))).decode("ascii")


def decrypt_body(cipher_b64: str) -> dict[str, Any]:
    """Giải response text/html (chuỗi base64)."""
    cipher = AES.new(_AES_KEY, AES.MODE_CBC, _AES_IV)
    raw = unpad(cipher.decrypt(base64.b64decode(cipher_b64)), 16)
    return json.loads(raw.decode("utf-8"))
