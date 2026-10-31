#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Replay đúng flow APP vừa bắt trong mitm
  red_packet_logs/app_grabred_mitm_1792394385.flow
  user thật: quangtoan98767

Thứ tự (host app.xoso66-d.com):
  1) POST /server/user/register
  2) POST /server/user/updatefundpassword   (MK rút 270288)
  3) GET  /server/user/banklist
  4) POST /server/user/bankcardbind

Header APP: x-device=android, x-app-platform="", x-app-version=8,
            x-app-build-version=991, x-requested-with=com.bet66.naapp
Referer: .../mobile/?platform=app&device=native
"""
from __future__ import annotations

import argparse
import json
import random
import string
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APP_BASE = "https://app.xoso66-d.com"
FUND_PW_DEFAULT = "270288"


def _rand_user(n: int = 12) -> str:
    return random.choice(string.ascii_lowercase) + "".join(
        random.choices(string.ascii_lowercase + string.digits, k=n - 1)
    )


def _rand_pass() -> str:
    chars = list(
        random.choice(string.ascii_uppercase)
        + random.choice(string.ascii_lowercase)
        + random.choice(string.digits)
        + "".join(random.choices(string.ascii_letters + string.digits, k=6))
    )
    random.shuffle(chars)
    return "".join(chars)


def _rand_phone() -> str:
    return "09" + "".join(random.choices(string.digits, k=8))


def _rand_holder() -> str:
    return random.choice(
        [
            "NGUYEN VAN AN",
            "TRAN MINH DUC",
            "LE THI HUONG",
            "PHAM QUANG HUY",
            "HOANG VAN LONG",
        ]
    )


def _rand_stk() -> str:
    return "".join(random.choices(string.digits, k=12))


def _guest(proxy: str) -> dict[str, Any]:
    from xoso66_red_packet import apply_client_profile
    from xoso66_register import new_guest_session

    s = new_guest_session(proxy=proxy)
    s["proxy"] = proxy
    s["base_url"] = APP_BASE
    s["crypto_version"] = "v3"  # APP mitm: cf-f-v=v3 + double AES
    apply_client_profile(s, "android_app")
    # Referer đúng APP native (mitm)
    hdr = dict(s.get("headers") or {})
    hdr["referer"] = f"{APP_BASE}/mobile/?platform=app&device=native"
    hdr["origin"] = APP_BASE
    s["headers"] = hdr
    return s


def _bootstrap(session: dict[str, Any]) -> None:
    """Lấy form-token + cek_p qua encryptKey — KHÔNG Playwright."""
    from xoso66_session import bootstrap_prelogin

    bootstrap_prelogin(session)
    if not session.get("cek_p"):
        raise RuntimeError("Không lấy được cek_p từ encryptKey trên app.xoso66-d.com")
    if not session.get("form_token"):
        raise RuntimeError("Không lấy được form-token")


def _register(session: dict[str, Any], plain: dict[str, Any]) -> dict[str, Any]:
    from xoso66_session import post_encrypted, merge_account
    from xoso66_register import is_register_success_response

    status, data, headers = post_encrypted(session, "/server/user/register", plain)
    ok, reason = is_register_success_response(
        data if isinstance(data, dict) else {},
        expected_username=str(plain.get("username") or ""),
    )
    if ok and isinstance(data, dict):
        user = data.get("data") if isinstance(data.get("data"), dict) else {}
        session["user_info"] = user
        session["username"] = user.get("username") or plain["username"]
        session["password"] = plain["password"]
        session["ukey"] = user.get("ukey")
        merge_account(session, {"user_info": user})
    return {
        "ok": ok,
        "http_status": status,
        "code": (data or {}).get("code") if isinstance(data, dict) else None,
        "msg": (data or {}).get("msg") if isinstance(data, dict) else None,
        "fail_reason": reason or None,
        "raw": data,
        "resp_headers": {k: headers.get(k) for k in headers if k.lower() in ("form-token", "cek-p")},
    }


def _fund(session: dict[str, Any], fund_password: str) -> dict[str, Any]:
    from xoso66_fund_password import (
        prepare_update_fund_password_payload,
        update_fund_password,
    )

    plain = prepare_update_fund_password_payload(fund_password)
    return update_fund_password(session, plain)


def _banklist(session: dict[str, Any]) -> dict[str, Any]:
    from xoso66_bank_bind import get_bank_list

    return get_bank_list(session)


def _bankbind(
    session: dict[str, Any],
    *,
    bank_name: str,
    cardnumber: str,
    truename: str,
    fund_password: str,
) -> dict[str, Any]:
    from xoso66_bank_bind import (
        bind_bank_card,
        get_bank_list,
        prepare_bankcard_bind_payload,
        resolve_bank_id,
    )

    banks = get_bank_list(session)
    if not banks.get("ok"):
        return {"ok": False, "msg": f"banklist fail: {banks.get('msg')}", "raw": banks}
    bid = resolve_bank_id(banks.get("banks") or [], bank_name=bank_name)
    plain = prepare_bankcard_bind_payload(
        bid, cardnumber, fund_password, truename=truename, branch=""
    )
    return bind_bank_card(session, plain)


def run(
    *,
    proxy: str,
    username: str,
    password: str,
    phone: str,
    holder: str,
    fund_password: str,
    bank: str,
    stk: str,
) -> dict[str, Any]:
    from xoso66_register import prepare_register_payload

    steps: list[dict[str, Any]] = []
    print(f"[REPLAY] host={APP_BASE}", flush=True)
    print(
        f"[REPLAY] user={username} fund={fund_password} bank={bank} stk={stk}",
        flush=True,
    )

    session = _guest(proxy)
    try:
        _bootstrap(session)
        print(
            f"[REPLAY] bootstrap ok form_token={bool(session.get('form_token'))} "
            f"cek_p={bool(session.get('cek_p'))}",
            flush=True,
        )
    except Exception as e:
        return {"ok": False, "steps": [{"step": "bootstrap", "ok": False, "msg": str(e)}]}

    plain = prepare_register_payload(
        username, password, phone=phone, truename=holder
    )
    print("[REPLAY] 1/4 POST /server/user/register …", flush=True)
    r1 = _register(session, plain)
    steps.append({"step": "register", **{k: v for k, v in r1.items() if k != "raw"}, "ok": r1.get("ok")})
    if not r1.get("ok"):
        steps[-1]["msg"] = r1.get("msg") or r1.get("fail_reason") or f"http={r1.get('http_status')}"
        steps[-1]["raw_preview"] = str(r1.get("raw"))[:300]
        return {"ok": False, "steps": steps, "session_keys": list(session.keys())}

    print("[REPLAY] 2/4 POST /server/user/updatefundpassword …", flush=True)
    r2 = _fund(session, fund_password)
    steps.append(
        {
            "step": "updatefundpassword",
            "ok": bool(r2.get("ok")),
            "code": r2.get("code"),
            "msg": r2.get("msg") or r2.get("fail_reason"),
        }
    )
    if not r2.get("ok"):
        return {"ok": False, "steps": steps}

    print("[REPLAY] 3/4 GET /server/user/banklist …", flush=True)
    r3 = _banklist(session)
    steps.append(
        {
            "step": "banklist",
            "ok": bool(r3.get("ok")),
            "code": r3.get("code"),
            "msg": r3.get("msg"),
            "n_banks": len(r3.get("banks") or []),
        }
    )
    if not r3.get("ok"):
        return {"ok": False, "steps": steps}

    print("[REPLAY] 4/4 POST /server/user/bankcardbind …", flush=True)
    r4 = _bankbind(
        session,
        bank_name=bank,
        cardnumber=stk,
        truename=holder,
        fund_password=fund_password,
    )
    steps.append(
        {
            "step": "bankcardbind",
            "ok": bool(r4.get("ok")),
            "code": r4.get("code"),
            "msg": r4.get("msg") or r4.get("fail_reason"),
        }
    )
    return {
        "ok": all(s.get("ok") for s in steps),
        "steps": steps,
        "username": username,
        "password": password,
        "fund_password": fund_password,
        "bank": bank,
        "stk": stk,
        "holder": holder,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default="")
    ap.add_argument("--fund-password", default=FUND_PW_DEFAULT)
    ap.add_argument("--bank", default="VPB")
    args = ap.parse_args()

    proxy = args.proxy
    if not proxy:
        from xoso66_accounts_db import list_accounts

        playing = [a for a in list_accounts() if a.get("status") == "Đang Chơi" and a.get("proxy")]
        proxy = str((playing[0] if playing else {}).get("proxy") or "")
    if not proxy:
        from xoso66_config_util import hardcoded_default_proxy

        proxy = hardcoded_default_proxy()
    if not proxy:
        print("Cần --proxy", flush=True)
        return 2

    out = run(
        proxy=proxy,
        username=_rand_user(),
        password=_rand_pass(),
        phone=_rand_phone(),
        holder=_rand_holder(),
        fund_password=args.fund_password,
        bank=args.bank,
        stk=_rand_stk(),
    )
    print("\n=== KẾT QUẢ REPLAY MITM FLOW ===", flush=True)
    for s in out.get("steps") or []:
        print(
            f"  [{'OK' if s.get('ok') else 'FAIL'}] {s.get('step')}: "
            f"{s.get('msg') or s.get('code') or ''}",
            flush=True,
        )
    print(f"  overall_ok={out.get('ok')}", flush=True)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str), flush=True)
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
