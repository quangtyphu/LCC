# -*- coding: utf-8 -*-
"""Chuẩn hóa mã ngân hàng (VPB → VPBank, …) — DB + bind site."""

from __future__ import annotations

import re

# key: compact lowercase input → (bank_code, bank_name hiển thị / DB)
_BANK_ALIASES: dict[str, tuple[str, str]] = {
    "vpb": ("vp_bank", "VPBank"),
    "vpbank": ("vp_bank", "VPBank"),
    "msb": ("msb", "MSB"),
    "vib": ("vib", "VIB"),
    "acb": ("acb", "ACB"),
    "vcb": ("viet_com_bank", "Vietcombank"),
    "vietcombank": ("viet_com_bank", "Vietcombank"),
    "tcb": ("tech_com_bank", "Techcombank"),
    "techcombank": ("tech_com_bank", "Techcombank"),
    "mb": ("mb_bank", "MBbank"),
    "mbbank": ("mb_bank", "MBbank"),
}


def normalize_bank(bank: str) -> tuple[str, str]:
    """
    Chuỗi ngân hàng từ form / CSV → (bank_code, bank_name).
    Ví dụ: VPB, vpb → (vp_bank, VPBank) — bind site vẫn qua alias trong resolve_bank_id.
    """
    s = (bank or "").strip()
    if not s:
        return "", ""
    key = re.sub(r"[\s_\-]+", "", s.lower())
    if key in _BANK_ALIASES:
        return _BANK_ALIASES[key]
    code = s.lower().replace(" ", "_")
    name = s.upper() if len(s) <= 8 and s.isalpha() else s
    return code, name
