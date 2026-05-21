# -*- coding: utf-8 -*-
"""Fingerprint nhẹ — không spoof canvas/WebGL (đủ tách session cơ bản)."""
from __future__ import annotations

import random
from typing import Any

_VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1280, "height": 720},
]

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
]

_LOCALES = ["vi-VN", "en-US", "en-GB"]


def random_fingerprint() -> dict[str, Any]:
    return {
        "viewport": random.choice(_VIEWPORTS),
        "user_agent": random.choice(_USER_AGENTS),
        "locale": random.choice(_LOCALES),
        "timezone_id": "Asia/Ho_Chi_Minh",
    }
