# -*- coding: utf-8 -*-
"""Test nạp + QR EMV TOPAY — python _test_deposit_qr.py"""
from __future__ import annotations

import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

from xoso66_accounts_db import init_db, username_for_log
from xoso66_deposit import (
    create_deposit_order,
    extract_topay_emv_payload,
    qr_image_to_data_uri,
    transfer_info_bank_fields,
    transfer_info_has_qr,
)
from xoso66_session import ensure_session


def main() -> int:
    init_db()
    aid = "acc20"
    user = username_for_log(aid)
    amount = 100_000
    print(f"=== Test nạp {user} ({aid}) {amount:,} VND ===")
    session = ensure_session(aid, force_login=False)
    rep = create_deposit_order(aid, amount, session=session)
    if not rep.get("ok"):
        print("FAIL:", rep.get("error"))
        return 1

    ti = rep.get("transfer_info") or {}
    bank = transfer_info_bank_fields(ti)
    emv = ti.get("qr_emv_payload") or extract_topay_emv_payload("")
    print("pay_url:", (rep.get("pay_url") or "")[:90])
    print("bank:", bank)
    print("has_qr:", transfer_info_has_qr(ti))
    print("qr_emv len:", len(str(ti.get("qr_emv_payload") or "")))
    print("qr_emv prefix:", str(ti.get("qr_emv_payload") or "")[:60], "...")
    print("qr_image_path:", rep.get("qr_image_path"))
    print("qr_url (should NOT be napas webp):", ti.get("qr_url"))

    b64 = qr_image_to_data_uri(
        qr_path=str(rep.get("qr_image_path") or ""),
        transfer_info=ti,
    )
    if not b64 or len(b64) < 500:
        print("FAIL: QR base64 quá ngắn hoặc trống")
        return 1
    if "napas" in b64.lower():
        print("FAIL: base64 có vẻ là logo napas")
        return 1
    print("OK: QR base64 length", len(b64))
    print("=== PASS ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
