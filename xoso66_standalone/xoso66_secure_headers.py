# -*- coding: utf-8 -*-
"""Header bảo vệ động của frontend XOSO66 (web 6.1.9)."""

from __future__ import annotations

import base64
import hashlib
import re
import time

_K_A = "EWMw3deRd7"
_K_B = "DciXrMfN44tr9EItTzrW1H"
_SEED = "A1b3D5"
CRYPTO_VERSION_HEADER = "cf-f-v"
CRYPTO_VERSION_V2 = "v2"


def _md5(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _normalize_url(url: str) -> str:
    return re.sub(r"([^:]/)/+", r"\1", str(url))


def generate_secure_headers(
    url: str,
    user_agent: str,
    *,
    now_ms: int | None = None,
) -> dict[str, str]:
    """Port ``getSecureHeaders(CryptoJS, url)`` của frontend hiện tại."""
    target = _normalize_url(url)
    stamp = str(int(now_ms if now_ms is not None else time.time() * 1000))
    ua = str(user_agent or "Mozilla/5.0")

    master = _md5(_K_A + _K_B + _SEED)
    joined = master + target
    left = _md5(joined) + _sha256(joined + stamp)
    right = _sha256(left + joined) + _sha256(joined + master)
    signature = _md5(left + right)[::-1]
    c_a_i = stamp[7:] + signature + stamp[:7]

    marker = _md5(stamp + _K_B)[:13]
    first = _sha256(target + stamp + target)
    second = _sha256(first + _K_A + marker)
    mixed = _md5(first + second + ua)
    packed = _b64(mixed[:16] + mixed[16:32][::-1] + marker)
    decoy_hash = _sha256(packed + _K_B)
    stamp_tail = stamp[7:]
    cf_pass = stamp_tail[:3] + decoy_hash[:48] + stamp_tail[3:]

    auth_1 = _md5(target + stamp)
    auth_2 = _sha256(auth_1 + "GET")
    auth_3 = _sha256(auth_2 + _K_A)
    auth_4 = _md5(auth_3 + _K_B)
    auth_mixed = auth_4[:10][::-1] + auth_4[10:22] + auth_4[22:32][::-1]
    auth_5 = _sha256(_b64(_b64(auth_mixed) + _K_B) + stamp)
    auth_6 = _md5(auth_5 + _K_A)
    auth_7 = _sha256(auth_6 + _K_B + target)
    auth_token = _sha256(auth_7 + _md5(ua)[:8])[:48]
    auth_stamp = (
        _md5(stamp[:5] + _K_B)[:4]
        + _md5(stamp[5:10] + _K_A)[:4]
        + _md5(stamp[10:] + _K_B)[:4]
    )
    auth_check = _md5(auth_token + auth_stamp)[:8]
    cf_auth_token = f"Bearer.{auth_token}.{auth_stamp}.{auth_check}"

    cf_con_s = _sha256(c_a_i + _md5(c_a_i + cf_pass) + _md5(cf_pass))
    return {
        "c-a-i": c_a_i,
        "cf-pass": cf_pass,
        "cf-auth-token": cf_auth_token,
        "cf-con-s": cf_con_s,
        CRYPTO_VERSION_HEADER: CRYPTO_VERSION_V2,
    }


def unpack_v2_ciphertext(value: str) -> str:
    """Bỏ chữ ký MD5 được frontend ``packCipherText`` chèn sau 3 ký tự."""
    packed = str(value or "")
    if len(packed) < 35:
        raise ValueError("v2 cipher quá ngắn")
    expected = packed[3:35]
    cipher = packed[:3] + packed[35:]
    if _md5(cipher) != expected:
        raise ValueError("v2 cipher sai chữ ký")
    return cipher


def pack_v2_ciphertext(value: str) -> str:
    """Chèn chữ ký MD5 theo frontend ``packCipherText``."""
    cipher = str(value or "")
    return cipher[:3] + _md5(cipher) + cipher[3:]
