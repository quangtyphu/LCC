# -*- coding: utf-8 -*-
"""
Rút tiền VND (ngân hàng) — XOSO66.

API: POST /server/payment/withdrawalorder (mã hóa + cek-k)

Payload (Vue submitWithdrawal, currency=1 VND):
  card_id       — id thẻ từ userbanklist (vd. 1735789)
  fund_password — MK rút 6 số
  money         — số tiền (chuỗi, không dấu phẩy)
  form_token    — từ session / getters.fromToken
  currency      — 1 = VND bank, 2 = USDT

CLI:
  python xoso66_withdraw.py -a acc1 --list-cards
  python xoso66_withdraw.py -a acc1 -m 100000 --fund-password 123456
  python xoso66_withdraw.py -a acc1 -m 50000 --fund-password 123456 --card-id 1735789
  python xoso66_withdraw.py -a acc1 -m 10000 --fund-password 123456 --browser
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from xoso66_bank_bind import get_user_bank_list
from xoso66_fund_password import (
    _parse_api_body,
    get_user_info,
    validate_fund_password,
)
from xoso66_session import (
    BASE_URL,
    ensure_session,
    merge_playwright_cookies,
    post_encrypted,
    _requests_session,
)
from xoso66_sessions_io import load_sessions, save_sessions

WITHDRAWAL_ORDER_PATH = "/server/payment/withdrawalorder"
CURRENCY_VND = 1
CURRENCY_USDT = 2

_WITHDRAW_PLAYWRIGHT_ACTIONS = ("userCenter/withdrawalorder",)


def prepare_withdrawal_payload(
    money: int | str,
    fund_password: str,
    *,
    card_id: int = 0,
    form_token: str = "",
    currency: int = CURRENCY_VND,
) -> dict[str, Any]:
    amt = str(money).strip().replace(",", "").replace(".", "")
    if not amt.isdigit() or int(amt) <= 0:
        raise ValueError("money phải là số nguyên dương")
    if not card_id:
        raise ValueError("Cần card_id (id thẻ trong userbanklist)")
    if not form_token:
        raise ValueError("Thiếu form_token trong session")
    return {
        "card_id": int(card_id),
        "fund_password": validate_fund_password(fund_password),
        "money": amt,
        "form_token": str(form_token),
        "currency": int(currency),
    }


def is_withdrawal_success(js: dict[str, Any]) -> tuple[bool, str]:
    if js.get("code") != 1:
        return False, str(js.get("msg") or f"code={js.get('code')}")
    return True, ""


def withdrawal_order(session: dict, plain: dict) -> dict[str, Any]:
    session.pop("aes_session_key", None)
    http = _requests_session(session)
    status, data, _ = post_encrypted(session, WITHDRAWAL_ORDER_PATH, plain, http=http)
    js = _parse_api_body(data)
    ok, reason = is_withdrawal_success(js)
    return {
        "ok": ok,
        "code": js.get("code"),
        "msg": js.get("msg"),
        "fail_reason": reason or None,
        "data": js.get("data"),
        "raw": js,
        "http_status": status,
        "session": session,
    }


def withdrawal_order_playwright(session: dict, plain: dict) -> dict[str, Any]:
    try:
        from xoso66_playwright_ctx import playwright_browser
    except ImportError:
        raise RuntimeError("pip install playwright && playwright install chromium")

    extra = {"accept": "application/json", "x-lang": "vi", "x-device": "pc"}
    for k in ("c-a-i", "cf-auth-token", "cf-con-s", "cf-pass"):
        v = (session.get("headers") or {}).get(k)
        if v:
            extra[k] = v
    ft = plain.get("form_token") or session.get("form_token")
    if ft:
        extra["form-token"] = ft

    js: Any = None
    with playwright_browser(session, base_url=BASE_URL, headless=True, extra_http_headers=extra) as (
        _p,
        _browser,
        context,
    ):
        page = context.new_page()
        page.goto(f"{BASE_URL}/home/", wait_until="domcontentloaded", timeout=90_000)
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            page.wait_for_timeout(6_000)
        page.wait_for_timeout(2_000)
        for action in _WITHDRAW_PLAYWRIGHT_ACTIONS:
            js = page.evaluate(
                """async ([action, body]) => {
                    const vm = document.querySelector('#app').__vue__;
                    try { return await vm.$store.dispatch(action, body); }
                    catch (e) { return { code: 0, msg: String(e) }; }
                }""",
                [action, plain],
            )
            if isinstance(js, dict) and js.get("code") == 1:
                break
        merge_playwright_cookies(session, context.cookies())

    if not isinstance(js, dict):
        return {"ok": False, "error": "response không hợp lệ", "raw": js, "session": session}
    ok, reason = is_withdrawal_success(js)
    return {
        "ok": ok,
        "code": js.get("code"),
        "msg": js.get("msg"),
        "fail_reason": reason or None,
        "data": js.get("data"),
        "raw": js,
        "session": session,
        "method": "playwright",
    }


def resolve_fund_password(session: dict, cli_password: str = "") -> str:
    """CLI --fund-password ưu tiên, không có thì đọc fund_password trong session JSON."""
    pwd = str(cli_password or "").strip()
    if pwd:
        return validate_fund_password(pwd)
    pwd = str(session.get("fund_password") or "").strip()
    if not pwd:
        raise ValueError(
            "Thiếu MK rút: --fund-password hoặc thêm fund_password vào xoso66_sessions.json"
        )
    return validate_fund_password(pwd)


def resolve_card_id(
    cards: list[dict], *, card_id: int = 0, session: dict | None = None
) -> int:
    if card_id:
        return int(card_id)
    if session and session.get("default_card_id"):
        return int(session["default_card_id"])
    if not cards:
        raise ValueError("Chưa có thẻ liên kết — chạy bank bind trước hoặc --list-cards")
    for c in cards:
        if c.get("is_default") in (1, "1", True):
            return int(c["id"])
    return int(cards[0]["id"])


def withdraw_for_account(
    account_id: str,
    amount: int | str,
    fund_password: str,
    *,
    card_id: int = 0,
    currency: int = CURRENCY_VND,
    use_playwright: bool = False,
    verify: bool = True,
) -> dict[str, Any]:
    session = ensure_session(account_id)
    from xoso66_deposit import get_form_token

    cards_resp = get_user_bank_list(session)
    if currency == CURRENCY_VND and not cards_resp.get("cards"):
        raise RuntimeError(
            "Không có thẻ NH — bind bank trước: python xoso66_bank_bind.py -a ... --list-linked"
        )

    cid = resolve_card_id(
        cards_resp.get("cards") or [], card_id=card_id, session=session
    )
    plain = prepare_withdrawal_payload(
        amount,
        fund_password,
        card_id=cid,
        form_token=get_form_token(session),
        currency=currency,
    )

    if use_playwright:
        result = withdrawal_order_playwright(session, plain)
    else:
        result = withdrawal_order(session, plain)
        if not result.get("ok") and result.get("code") in (1004, 10058, None):
            result = withdrawal_order_playwright(session, plain)

    if verify:
        info = get_user_info(session)
        bal = (info.get("data") or {}).get("money")
        result["balance_after"] = bal
        result["card_id_used"] = cid

    accounts = load_sessions()
    accounts[account_id] = session
    save_sessions(accounts)
    result["account_id"] = account_id
    result["payload_sent"] = {
        k: v for k, v in plain.items() if k != "fund_password"
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="XOSO66 rút tiền (VND bank)")
    parser.add_argument("-a", "--account", required=True)
    parser.add_argument("-m", "--money", type=int, default=0, help="số tiền rút")
    parser.add_argument("--fund-password", default="", help="MK rút 6 số")
    parser.add_argument("--card-id", type=int, default=0, help="id thẻ userbanklist")
    parser.add_argument("--currency", type=int, default=CURRENCY_VND, choices=(1, 2))
    parser.add_argument("--list-cards", action="store_true")
    parser.add_argument("--browser", action="store_true", help="ép Playwright")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="chỉ in payload (không gọi API)",
    )
    args = parser.parse_args()

    try:
        session = ensure_session(args.account)

        if args.list_cards:
            out = get_user_bank_list(session)
            print(
                json.dumps(
                    {"ok": out.get("ok"), "cards": out.get("cards"), "raw": out.get("raw")},
                    indent=2,
                    ensure_ascii=False,
                )
            )
            save_sessions({**load_sessions(), args.account: session})
            return 0 if out.get("ok") else 1

        fund_pwd = resolve_fund_password(session, args.fund_password) if (
            args.money or args.dry_run
        ) else ""

        if args.dry_run:
            from xoso66_deposit import get_form_token

            cards = get_user_bank_list(session).get("cards") or []
            cid = resolve_card_id(cards, card_id=args.card_id, session=session)
            plain = prepare_withdrawal_payload(
                args.money or 10000,
                fund_pwd,
                card_id=cid,
                form_token=get_form_token(session),
                currency=args.currency,
            )
            print(json.dumps({"payload": plain, "cards": cards}, indent=2, ensure_ascii=False))
            return 0

        if not args.money:
            parser.print_help()
            return 1

        result = withdraw_for_account(
            args.account,
            args.money,
            fund_pwd,
            card_id=args.card_id,
            currency=args.currency,
            use_playwright=args.browser,
        )
        print(
            json.dumps(
                {
                    "ok": result.get("ok"),
                    "code": result.get("code"),
                    "msg": result.get("msg"),
                    "fail_reason": result.get("fail_reason"),
                    "card_id": result.get("card_id_used"),
                    "balance_after": result.get("balance_after"),
                    "data": result.get("data"),
                    "method": result.get("method", "http"),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0 if result.get("ok") else 1
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, indent=2, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
