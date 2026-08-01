# -*- coding: utf-8 -*-
"""CMS provision: đăng ký → (MK rút) → (bind bank) — thành công bước nào lưu bước đó.

Bank (bank_code, bank_name, account_number) chỉ ghi DB khi bind bank thành công.
"""

from __future__ import annotations

from typing import Any

from xoso66_accounts_db import (
    append_provision_log,
    create_account,
    get_account,
    get_account_by_username,
    save_session_runtime,
    update_account,
)
from xoso66_banks import normalize_bank
from xoso66_proxy import ProxyRequiredError, require_explicit_proxy
from xoso66_register import (
    prepare_register_payload,
    register_account_playwright,
    register_failure_message,
    _resolve_cms_register_context,
)
from xoso66_session import login_account
from xoso66_sessions_io import merge_account

PROVISION_STEPS = ("register", "fund_password", "bank_bind")


def _step(name: str, ok: bool, **extra: Any) -> dict[str, Any]:
    return {"step": name, "ok": ok, **extra}


def _parse_body(body: dict[str, Any]) -> dict[str, Any]:
    proxy = require_explicit_proxy(body.get("proxy"))
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    phone = str(body.get("phone") or "").strip()
    account_holder = str(body.get("account_holder") or body.get("truename") or "").strip()
    fund_password = str(body.get("fund_password") or "").strip()
    account_number = str(body.get("account_number") or "").strip()
    bank = str(
        body.get("bank")
        or body.get("bank_name")
        or body.get("bank_code")
        or ""
    ).strip()
    bank_code, bank_name = normalize_bank(bank)
    account_id = str(body.get("account_id") or "").strip() or None
    device = str(body.get("device") or "").strip()
    vip_level = str(body.get("vip_level") or "").strip()
    step = str(body.get("step") or "all").strip().lower()

    if step not in ("all", *PROVISION_STEPS):
        raise ValueError(f"step không hợp lệ: {step}")

    return {
        "proxy": proxy,
        "username": username,
        "password": password,
        "phone": phone,
        "account_holder": account_holder,
        "fund_password": fund_password,
        "account_number": account_number,
        "bank_code": bank_code,
        "bank_name": bank_name,
        "device": device,
        "vip_level": vip_level,
        "account_id": account_id,
        "step": step,
    }


def _provision_skip_existing(existing: dict[str, Any], *, step: str = "register") -> dict[str, Any]:
    """Username đã có trong DB — không gọi site, bỏ qua."""
    aid = str(existing.get("id") or "")
    st = _step(
        step,
        True,
        skipped=True,
        msg=f"username đã có trong DB ({aid})",
    )
    print(f"[PROVISION] Bỏ qua {existing.get('username')} — đã có DB {aid}", flush=True)
    return {
        "ok": True,
        "skipped": True,
        "step": step,
        "account_id": aid,
        "steps": [st],
        "saved_to_db": True,
        "account": existing,
    }


def _validate_register_fields(p: dict[str, Any]) -> None:
    if not p["username"] or not p["password"]:
        raise ValueError("username và password bắt buộc")
    if not p["phone"] or len(p["phone"]) != 10 or not p["phone"].isdigit():
        raise ValueError("phone phải 10 chữ số")
    if not p["account_holder"]:
        raise ValueError("account_holder bắt buộc")


def _provision_register(p: dict[str, Any]) -> dict[str, Any]:
    _validate_register_fields(p)
    existing = get_account_by_username(p["username"])
    if existing:
        return _provision_skip_existing(existing, step="register")
    print(f"[PROVISION] Đăng ký {p['username']}…", flush=True)

    plain = prepare_register_payload(
        p["username"],
        p["password"],
        phone=p["phone"],
        truename=p["account_holder"],
    )

    cms_device = str(p.get("device") or "").strip()
    try:
        proxy, cms_row = _resolve_cms_register_context(cms_device=cms_device, proxy=p["proxy"])
        if cms_row and cms_row.get("proxy"):
            p["proxy"] = proxy
    except ValueError as e:
        return {
            "ok": False,
            "step": "register",
            "steps": [_step("register", False, msg=str(e))],
            "saved_to_db": False,
        }

    try:
        reg = register_account_playwright(plain, proxy=p["proxy"], cms_device=cms_device)
    except ProxyRequiredError as e:
        return {
            "ok": False,
            "step": "register",
            "steps": [_step("register", False, msg=str(e))],
            "saved_to_db": False,
        }
    except Exception as e:
        return {
            "ok": False,
            "step": "register",
            "steps": [_step("register", False, msg=str(e))],
            "saved_to_db": False,
        }

    sess = reg.get("session") or {}
    if not reg.get("ok"):
        return {
            "ok": False,
            "step": "register",
            "steps": [
                _step(
                    "register",
                    False,
                    msg=register_failure_message(reg),
                    code=reg.get("code"),
                )
            ],
            "saved_to_db": False,
        }

    try:
        login_patch = login_account(sess)
        merge_account(sess, login_patch)
    except Exception as e:
        return {
            "ok": False,
            "step": "register",
            "steps": [_step("register", False, msg=f"login sau đăng ký: {e}")],
            "saved_to_db": False,
            "session": sess,
        }

    try:
        row = create_account(
            {
                "username": p["username"],
                "password": p["password"],
                "phone": p["phone"],
                "account_holder": p["account_holder"],
                "proxy": p["proxy"],
                "device": p["device"],
                "vip_level": p["vip_level"],
                "fund_password": "",
                "status": "registered",
                "session_json": sess,
            }
        )
        saved_id = row["id"]
        save_session_runtime(saved_id, sess)
        st = _step("register", True, msg=reg.get("msg") or "Đăng ký tài khoản thành công")
        append_provision_log(saved_id, st)
    except Exception as e:
        return {
            "ok": False,
            "step": "register",
            "steps": [_step("register", False, msg=str(e))],
            "saved_to_db": False,
            "session": sess,
        }

    acc = get_account(saved_id)
    return {
        "ok": True,
        "step": "register",
        "account_id": saved_id,
        "steps": [st],
        "saved_to_db": True,
        "account": acc,
    }


def _provision_fund_password(p: dict[str, Any]) -> dict[str, Any]:
    account_id = p["account_id"]
    if not account_id:
        raise ValueError("account_id bắt buộc cho bước fund_password")
    if not p["fund_password"]:
        return {
            "ok": True,
            "step": "fund_password",
            "account_id": account_id,
            "steps": [_step("fund_password", True, skipped=True, msg="không có MK rút")],
            "saved_to_db": True,
        }

    print(f"[PROVISION] Đặt MK rút {account_id}…", flush=True)
    from xoso66_fund_password import set_fund_password_for_account

    try:
        fp = set_fund_password_for_account(
            account_id,
            p["fund_password"],
            use_playwright=True,
        )
        if fp.get("ok"):
            update_account(
                account_id,
                {"fund_password": p["fund_password"], "status": "fund_password_ok"},
            )
            st = _step("fund_password", True, msg=fp.get("msg") or "Đặt mk rút tiền thành công")
        else:
            st = _step(
                "fund_password",
                False,
                msg=fp.get("msg") or fp.get("fail_reason") or "Đặt mk rút tiền thất bại",
            )
    except Exception as e:
        st = _step("fund_password", False, msg=str(e))

    append_provision_log(account_id, st)
    acc = get_account(account_id)
    return {
        "ok": st["ok"],
        "step": "fund_password",
        "account_id": account_id,
        "steps": [st],
        "saved_to_db": True,
        "account": acc,
    }


def _provision_bank_bind(p: dict[str, Any]) -> dict[str, Any]:
    account_id = p["account_id"]
    if not account_id:
        raise ValueError("account_id bắt buộc cho bước bank_bind")
    if not p["account_holder"]:
        acc0 = get_account(account_id)
        if acc0:
            p["account_holder"] = str(acc0.get("account_holder") or "")
    if not p["account_number"]:
        return {
            "ok": True,
            "step": "bank_bind",
            "account_id": account_id,
            "steps": [_step("bank_bind", True, skipped=True, msg="không có STK")],
            "saved_to_db": True,
        }
    if not p["fund_password"]:
        st = _step("bank_bind", False, msg="cần fund_password trước khi bind")
        append_provision_log(account_id, st)
        return {
            "ok": False,
            "step": "bank_bind",
            "account_id": account_id,
            "steps": [st],
            "saved_to_db": True,
        }

    print(f"[PROVISION] Liên kết bank {account_id}…", flush=True)
    from xoso66_bank_bind import bind_bank_for_account

    bank_label = p["bank_name"] or p["bank_code"]
    branch = p.get("device") or ""
    if not branch:
        acc_dev = get_account(account_id)
        if acc_dev:
            branch = str(acc_dev.get("device") or "")
    try:
        bk = bind_bank_for_account(
            account_id,
            bank_name=bank_label,
            cardnumber=p["account_number"],
            truename=p["account_holder"],
            fund_password=p["fund_password"],
            branch=branch,
            use_playwright=True,
        )
        if bk.get("ok"):
            cards = bk.get("linked_cards") or []
            card_id = None
            for card in cards:
                if isinstance(card, dict) and card.get("card_id"):
                    card_id = card["card_id"]
                    break
            patch: dict[str, Any] = {
                "account_number": p["account_number"],
                "bank_code": p["bank_code"],
                "bank_name": p["bank_name"] or p["bank_code"],
                "status": "bank_linked",
            }
            if card_id:
                patch["default_card_id"] = card_id
            if cards:
                patch["linked_banks"] = cards
            update_account(account_id, patch)
            if bk.get("session"):
                save_session_runtime(account_id, bk["session"])
            st = _step(
                "bank_bind",
                True,
                default_card_id=card_id,
                msg="Liên kết bank thành công",
            )
        else:
            st = _step(
                "bank_bind",
                False,
                msg=bk.get("msg") or bk.get("fail_reason") or "Liên kết bank thất bại",
            )
            acc0 = get_account(account_id)
            if acc0 and acc0.get("status") != "bank_linked":
                update_account(
                    account_id,
                    {"bank_code": "", "bank_name": "", "account_number": ""},
                )
    except Exception as e:
        st = _step("bank_bind", False, msg=str(e))
        acc0 = get_account(account_id)
        if acc0 and acc0.get("status") != "bank_linked":
            update_account(
                account_id,
                {"bank_code": "", "bank_name": "", "account_number": ""},
            )

    append_provision_log(account_id, st)
    acc = get_account(account_id)
    return {
        "ok": st["ok"],
        "step": "bank_bind",
        "account_id": account_id,
        "steps": [st],
        "saved_to_db": True,
        "account": acc,
    }


def provision_account(body: dict[str, Any]) -> dict[str, Any]:
    """
    body: username, password, phone, account_holder, proxy (bắt buộc khi đăng ký),
          fund_password?, account_number?, bank / bank_name?,
          step?: register | fund_password | bank_bind | all (mặc định all),
          account_id?: bắt buộc từ bước 2 trở đi
    """
    p = _parse_body(body)
    step = p["step"]

    if p.get("username") and step in ("register", "all"):
        existing = get_account_by_username(p["username"])
        if existing:
            return _provision_skip_existing(existing, step=step)

    if step == "register":
        return _provision_register(p)
    if step == "fund_password":
        return _provision_fund_password(p)
    if step == "bank_bind":
        return _provision_bank_bind(p)

    # all — chạy tuần tự 3 bước (một request)
    out = _provision_register(p)
    all_steps = list(out.get("steps") or [])
    if not out.get("ok"):
        return {**out, "steps": all_steps}

    account_id = out["account_id"]
    p["account_id"] = account_id

    if p["fund_password"]:
        fp_out = _provision_fund_password(p)
        all_steps.extend(fp_out.get("steps") or [])
        if not fp_out.get("ok"):
            acc = get_account(account_id)
            return {
                "ok": False,
                "account_id": account_id,
                "steps": all_steps,
                "saved_to_db": True,
                "account": acc,
            }

    if p["account_number"]:
        bk_out = _provision_bank_bind(p)
        all_steps.extend(bk_out.get("steps") or [])
        ok = bk_out.get("ok", False)
    else:
        ok = True

    acc = get_account(account_id)
    return {
        "ok": out.get("ok") and ok,
        "account_id": account_id,
        "steps": all_steps,
        "saved_to_db": True,
        "account": acc,
    }
