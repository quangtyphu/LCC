# -*- coding: utf-8 -*-
"""Lưu tài khoản từ CMS: nếu DB chưa có MK rút / bank thì gọi API site trước."""

from __future__ import annotations

import os
from typing import Any

from xoso66_accounts_db import get_account, save_session_runtime, update_account
from xoso66_banks import normalize_bank

DEFAULT_FUND_PASSWORD = (os.environ.get("XOSO66_DEFAULT_FUND_PASSWORD") or "270288").strip()

_SITE_ONLY_KEYS = frozenset({"fund_password", "bank_code", "bank_name", "account_number"})


def _step(name: str, ok: bool, **extra: Any) -> dict[str, Any]:
    return {"step": name, "ok": ok, **extra}


def _db_has_fund_password(row: dict[str, Any]) -> bool:
    return bool(str(row.get("fund_password") or "").strip())


def _db_has_bank(row: dict[str, Any]) -> bool:
    if str(row.get("status") or "") == "bank_linked":
        return True
    return bool(
        str(row.get("bank_name") or "").strip()
        or str(row.get("bank_code") or "").strip()
    )


def _bank_fields(body: dict[str, Any]) -> tuple[str, str, str]:
    bank = str(
        body.get("bank_name")
        or body.get("bank")
        or body.get("bank_code")
        or ""
    ).strip()
    if not bank:
        return "", "", ""
    code, name = normalize_bank(bank)
    return code, name, str(body.get("account_number") or "").strip()


def save_account_with_site_sync(
    account_id: str, body: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """
    Cập nhật DB; nếu DB chưa có MK rút / bank mà form có đủ thì gọi site trước.
    Bank / fund_password chỉ ghi DB khi API site thành công (hoặc đã có sẵn trong DB).
    """
    cur = get_account(account_id)
    if not cur:
        raise KeyError(f"Không có account '{account_id}'")

    sync_steps: list[dict[str, Any]] = []
    patch: dict[str, Any] = {}
    for k, v in body.items():
        if k in ("id", "bank"):
            continue
        if k in _SITE_ONLY_KEYS:
            continue
        if v is not None and v != "":
            patch[k] = v

    fund_pw = str(body.get("fund_password") or "").strip()
    bank_code, bank_name, account_number = _bank_fields(body)
    branch = str(body.get("branch") or body.get("device") or "").strip()
    holder = str(
        body.get("account_holder") or cur.get("account_holder") or ""
    ).strip()

    if fund_pw and not _db_has_fund_password(cur):
        print(f"[SAVE] Đặt MK rút site {account_id}…", flush=True)
        from xoso66_fund_password import set_fund_password_for_account

        try:
            fp = set_fund_password_for_account(
                account_id, fund_pw, use_playwright=True
            )
        except Exception as e:
            sync_steps.append(_step("fund_password", False, msg=str(e)))
        else:
            if fp.get("ok"):
                patch["fund_password"] = fund_pw
                if str(cur.get("status") or "") not in ("bank_linked",):
                    patch["status"] = "fund_password_ok"
                if fp.get("session"):
                    save_session_runtime(account_id, fp["session"])
                sync_steps.append(
                    _step("fund_password", True, msg=fp.get("msg") or "OK")
                )
                cur = get_account(account_id) or cur
            else:
                sync_steps.append(
                    _step(
                        "fund_password",
                        False,
                        msg=fp.get("msg") or fp.get("fail_reason") or "thất bại",
                    )
                )

    fp_for_bind = str(
        patch.get("fund_password")
        or cur.get("fund_password")
        or fund_pw
        or ""
    ).strip()

    need_bank = account_number and bank_name and not _db_has_bank(cur)
    if need_bank:
        if not fp_for_bind:
            sync_steps.append(
                _step("bank_bind", False, msg="cần MK rút 6 số để liên kết bank")
            )
        else:
            print(f"[SAVE] Liên kết bank site {account_id}…", flush=True)
            from xoso66_bank_bind import bind_bank_for_account

            label = bank_name or bank_code
            try:
                bk = bind_bank_for_account(
                    account_id,
                    bank_name=label,
                    cardnumber=account_number,
                    truename=holder,
                    fund_password=fp_for_bind,
                    branch=branch,
                    use_playwright=True,
                )
            except Exception as e:
                sync_steps.append(_step("bank_bind", False, msg=str(e)))
            else:
                if bk.get("ok"):
                    patch["account_number"] = account_number
                    patch["bank_code"] = bank_code
                    patch["bank_name"] = bank_name or bank_code
                    patch["status"] = "bank_linked"
                    cards = bk.get("linked_cards") or []
                    for card in cards:
                        if isinstance(card, dict) and card.get("card_id"):
                            patch["default_card_id"] = card["card_id"]
                            break
                    if cards:
                        patch["linked_banks"] = cards
                    if bk.get("session"):
                        save_session_runtime(account_id, bk["session"])
                    sync_steps.append(
                        _step("bank_bind", True, msg="Liên kết bank thành công")
                    )
                else:
                    sync_steps.append(
                        _step(
                            "bank_bind",
                            False,
                            msg=bk.get("msg")
                            or bk.get("fail_reason")
                            or "Liên kết bank thất bại",
                        )
                    )
    elif _db_has_bank(cur):
        if bank_code:
            patch["bank_code"] = bank_code
        if bank_name:
            patch["bank_name"] = bank_name
        if account_number:
            patch["account_number"] = account_number
    elif fund_pw and _db_has_fund_password(cur):
        patch["fund_password"] = fund_pw

    if not patch:
        return cur, sync_steps

    row = update_account(account_id, patch)
    return row, sync_steps
