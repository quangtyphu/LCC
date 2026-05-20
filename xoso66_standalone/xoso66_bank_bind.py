# -*- coding: utf-8 -*-
"""
Liên kết tài khoản ngân hàng VND — XOSO66.

API:
  GET  /server/user/banklist           — danh sách NH (id, name, code)
  GET  /server/user/addresslist        — tỉnh/huyện (mở form, plain)
  POST /server/user/bankcardbind       — thêm thẻ (mã hóa + cek-k)
  GET  /server/user/userbanklist       — thẻ đã liên kết (cek-k; sau bind)
  GET  /server/user/useraddresslist    — địa chỉ user (cek-k; sau bind)
  GET  /server/user/userbanklist?check_order=1 — refresh thẻ sau bind

Payload bankcardbind (Vue sendData):
  bank_id, cardnumber, branch, fund_password (6 số), truename, sms_code

CLI:
  python xoso66_bank_bind.py -a acc1 --list-banks
  python xoso66_bank_bind.py -a acc1 --list-linked
  python xoso66_bank_bind.py -a acc1 --list-addresses
  python xoso66_bank_bind.py -a acc1 --list-linked --check-order
  python xoso66_bank_bind.py -a acc1 --refresh-after-bind
  python xoso66_bank_bind.py -a acc1 --bank ACB --card 123456789 --name "NGUYEN VAN A" --fund-password 123456
  python xoso66_bank_bind.py -a acc1 --bank-id 2 --account 123456789 --fund-password 123456
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from xoso66_fund_password import _parse_api_body, get_user_info
from xoso66_session import (
    BASE_URL,
    ensure_session,
    merge_playwright_cookies,
    post_encrypted,
    _merge_response_cookies,
    _requests_session,
)
from xoso66_sessions_io import load_sessions, save_sessions

_ENCRYPTED_GET_FALLBACK_CODES = (1004, 10055, 10058)
_USER_BANKLIST_ACTIONS = ("user/getUserBanklist", "userCenter/getUserBanklist")
_USER_ADDRESSLIST_ACTIONS = (
    "user/getUserAddresslist",
    "userCenter/getUserAddresslist",
    "user/getUserUsdtaddresslist",
    "userCenter/getUserUsdtaddresslist",
)


def _get_encrypted(session: dict, path: str, params: dict | None = None) -> tuple[int, Any]:
    """GET endpoint có cek-k (vd. userbanklist): params plaintext + header cek-k."""
    from xoso66_deposit import (
        apply_response_tokens,
        build_request_headers,
        decrypt_deposit_body,
        encrypt_deposit_body,
        get_form_token,
    )

    params = params if params is not None else {}
    session.pop("aes_session_key", None)
    _, cek_k, aes_key = encrypt_deposit_body(session, params)
    headers = build_request_headers(session, cek_k=cek_k, form_token=get_form_token(session))
    r = _requests_session(session).get(
        f"{BASE_URL}{path}",
        params=params or None,
        headers=headers,
        timeout=30,
    )
    apply_response_tokens(session, r.headers)
    _merge_response_cookies(session, r)
    text = r.text
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1]
    if r.status_code == 200 and text:
        try:
            return r.status_code, decrypt_deposit_body(session, text, aes_key, dict(r.headers))
        except Exception as e:
            return r.status_code, {"_decrypt_error": str(e), "_cipher_preview": text[:200]}
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"_raw": text[:400]}


def _encrypted_get_to_result(session: dict, path: str, params: dict | None) -> dict[str, Any]:
    status, data = _get_encrypted(session, path, params or {})
    js = _parse_api_body(data)
    return {
        "ok": js.get("code") == 1,
        "code": js.get("code"),
        "msg": js.get("msg"),
        "data": js.get("data"),
        "raw": js,
        "http_status": status,
    }


def _playwright_store_dispatch(
    session: dict,
    actions: tuple[str, ...],
    payload: dict | None = None,
) -> dict[str, Any]:
    try:
        from xoso66_playwright_ctx import playwright_browser
    except ImportError:
        raise RuntimeError("pip install playwright && playwright install chromium")

    body = payload or {}
    extra = {"accept": "application/json", "x-lang": "vi", "x-device": "pc"}
    for k in ("c-a-i", "cf-auth-token", "cf-con-s", "cf-pass"):
        v = (session.get("headers") or {}).get(k)
        if v:
            extra[k] = v
    if session.get("form_token"):
        extra["form-token"] = session["form_token"]

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
        for action in actions:
            js = page.evaluate(
                """async ([action, body]) => {
                    const vm = document.querySelector('#app').__vue__;
                    try { return await vm.$store.dispatch(action, body); }
                    catch (e) { return { code: 0, msg: String(e) }; }
                }""",
                [action, body],
            )
            if isinstance(js, dict) and js.get("code") == 1:
                break
        merge_playwright_cookies(session, context.cookies())

    if not isinstance(js, dict):
        return {"ok": False, "error": "response không hợp lệ", "raw": js, "method": "playwright"}
    return {
        "ok": js.get("code") == 1,
        "code": js.get("code"),
        "msg": js.get("msg"),
        "data": js.get("data"),
        "raw": js,
        "method": "playwright",
    }


def _encrypted_get_with_fallback(
    session: dict,
    path: str,
    params: dict | None,
    *,
    use_playwright: bool,
    playwright_actions: tuple[str, ...],
    parse: Any,
) -> dict[str, Any]:
    if use_playwright:
        return parse(_playwright_store_dispatch(session, playwright_actions, params))
    out = _encrypted_get_to_result(session, path, params)
    if not out.get("ok") and out.get("code") in _ENCRYPTED_GET_FALLBACK_CODES:
        return parse(_playwright_store_dispatch(session, playwright_actions, params))
    return parse(out)


BANKLIST_PATH = "/server/user/banklist"
ADDRESSLIST_PATH = "/server/user/addresslist"
BANKCARDBIND_PATH = "/server/user/bankcardbind"
USER_BANKLIST_PATH = "/server/user/userbanklist"
USER_ADDRESSLIST_PATH = "/server/user/useraddresslist"


def _auth_headers(session: dict, *, content_type: str = "application/json") -> dict[str, str]:
    from xoso66_deposit import build_common_headers, get_form_token

    return build_common_headers(session, form_token=get_form_token(session), content_type=content_type)


def _get_json(session: dict, path: str, *, params: dict | None = None) -> dict[str, Any]:
    from xoso66_deposit import apply_response_tokens

    r = _requests_session(session).get(
        f"{BASE_URL}{path}",
        headers=_auth_headers(session),
        params=params or {},
        timeout=30,
    )
    apply_response_tokens(session, r.headers)
    _merge_response_cookies(session, r)
    try:
        js = r.json()
    except Exception:
        return {"ok": False, "http_status": r.status_code, "raw": r.text[:500]}
    data = js.get("data")
    return {"ok": js.get("code") == 1, "code": js.get("code"), "msg": js.get("msg"), "data": data, "raw": js}


def get_bank_list(session: dict) -> dict[str, Any]:
    """GET /server/user/banklist."""
    out = _get_json(session, BANKLIST_PATH)
    banks = out.get("data") if isinstance(out.get("data"), list) else []
    out["banks"] = banks
    return out


def get_address_list(session: dict) -> dict[str, Any]:
    """GET /server/user/addresslist."""
    return _get_json(session, ADDRESSLIST_PATH)


def get_user_bank_list(session: dict, *, params: dict | None = None, use_playwright: bool = False) -> dict[str, Any]:
    """GET /server/user/userbanklist — thẻ đã liên kết (cek-k). params: {} hoặc {check_order: 1}."""
    return _encrypted_get_with_fallback(
        session,
        USER_BANKLIST_PATH,
        params or {},
        use_playwright=use_playwright,
        playwright_actions=_USER_BANKLIST_ACTIONS,
        parse=_cards_from_list_response,
    )


def get_user_address_list(session: dict, *, params: dict | None = None, use_playwright: bool = False) -> dict[str, Any]:
    """GET /server/user/useraddresslist — địa chỉ đã lưu (cek-k)."""
    return _encrypted_get_with_fallback(
        session,
        USER_ADDRESSLIST_PATH,
        params or {},
        use_playwright=use_playwright,
        playwright_actions=_USER_ADDRESSLIST_ACTIONS,
        parse=_addresses_from_list_response,
    )


def refresh_after_bank_bind(session: dict, *, use_playwright: bool = False) -> dict[str, Any]:
    """
    Giống trình duyệt sau bankcardbind thành công:
      userbanklist → useraddresslist → userbanklist?check_order=1
    """
    return {
        "userbanklist": get_user_bank_list(session, params={}, use_playwright=use_playwright),
        "useraddresslist": get_user_address_list(session, params={}, use_playwright=use_playwright),
        "userbanklist_check_order": get_user_bank_list(
            session, params={"check_order": 1}, use_playwright=use_playwright
        ),
    }


def _cards_from_list_response(out: dict[str, Any]) -> dict[str, Any]:
    data = out.get("data")
    if isinstance(data, list):
        out["cards"] = data
    elif isinstance(data, dict):
        out["cards"] = data.get("list") or data.get("banklist") or []
        out["fast_money"] = data.get("fast_money")
    elif not out.get("cards"):
        out["cards"] = []
    return out


def _addresses_from_list_response(out: dict[str, Any]) -> dict[str, Any]:
    data = out.get("data")
    if isinstance(data, list):
        out["addresses"] = data
    elif isinstance(data, dict):
        out["addresses"] = data.get("list") or data.get("addresslist") or []
    elif not out.get("addresses"):
        out["addresses"] = []
    return out


def _expand_bank_query(bank_name: str) -> list[str]:
    """Chuỗi tra cứu (lowercase) — gồm alias mã nội bộ (VPB) → banklist site."""
    s = (bank_name or "").strip().lower()
    if not s:
        return []
    keys = {s, s.replace(" ", "_"), s.replace("_", "")}
    compact = s.replace(" ", "").replace("_", "")
    aliases: dict[str, set[str]] = {
        "vpb": {"vpbank", "vp_bank"},
        "vpbank": {"vp_bank"},
        "vcb": {"vietcombank", "viet_com_bank"},
        "tcb": {"techcombank", "tech_com_bank"},
        "mb": {"mbbank", "mb_bank"},
    }
    if compact in aliases:
        keys |= aliases[compact]
    return list(keys)


def resolve_bank_id(banks: list[dict], *, bank_id: int = 0, bank_name: str = "") -> int:
    if bank_id:
        return int(bank_id)
    queries = _expand_bank_query(bank_name)
    if not queries:
        raise ValueError("Cần --bank-id hoặc --bank (tên NH, VD: ACB)")
    for b in banks:
        bn = str(b.get("name", "")).lower()
        bc = str(b.get("code", "")).lower()
        for q in queries:
            if bn == q or bc == q or bc == q.replace(" ", "_"):
                return int(b["id"])
    raise ValueError(f"Không tìm thấy ngân hàng '{bank_name}' trong banklist")


def validate_card_number(num: str) -> str:
    s = str(num).strip().replace(" ", "").replace("-", "")
    if not s.isdigit():
        raise ValueError("Số tài khoản chỉ gồm chữ số")
    if len(s) < 5 or len(s) > 35:
        raise ValueError("Số tài khoản phải từ 5–35 chữ số (theo form site)")
    return s


def validate_fund_password(pwd: str) -> str:
    s = str(pwd).strip()
    if len(s) != 6 or not s.isdigit():
        raise ValueError("Mật khẩu rút tiền (fund_password) phải đúng 6 chữ số")
    return s


def prepare_bankcard_bind_payload(
    bank_id: int,
    cardnumber: str,
    fund_password: str,
    *,
    truename: str = "",
    branch: str = "",
    sms_code: str = "",
) -> dict[str, Any]:
    return {
        "bank_id": int(bank_id),
        "cardnumber": validate_card_number(cardnumber),
        "branch": str(branch or ""),
        "fund_password": validate_fund_password(fund_password),
        "truename": str(truename or ""),
        "sms_code": str(sms_code or ""),
    }


def is_bank_bind_success(js: dict[str, Any]) -> tuple[bool, str]:
    if js.get("code") != 1:
        return False, str(js.get("msg") or f"code={js.get('code')}")
    return True, ""


def bind_bank_card(session: dict, plain: dict) -> dict[str, Any]:
    session.pop("aes_session_key", None)
    http = _requests_session(session)
    status, data, _ = post_encrypted(session, BANKCARDBIND_PATH, plain, http=http)
    js = _parse_api_body(data)
    ok, reason = is_bank_bind_success(js)
    return {
        "ok": ok,
        "code": js.get("code"),
        "msg": js.get("msg"),
        "fail_reason": reason or None,
        "raw": js,
        "http_status": status,
        "session": session,
    }


def bind_bank_card_playwright(session: dict, plain: dict) -> dict[str, Any]:
    try:
        from xoso66_playwright_ctx import playwright_browser
    except ImportError:
        raise RuntimeError("pip install playwright && playwright install chromium")

    extra = {"accept": "application/json", "x-lang": "vi", "x-device": "pc"}
    for k in ("c-a-i", "cf-auth-token", "cf-con-s", "cf-pass"):
        v = (session.get("headers") or {}).get(k)
        if v:
            extra[k] = v
    if session.get("form_token"):
        extra["form-token"] = session["form_token"]

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
        js = page.evaluate(
            """async (body) => {
                const vm = document.querySelector('#app').__vue__;
                return await vm.$store.dispatch('userCenter/bankcardbind', body);
            }""",
            plain,
        )
        merge_playwright_cookies(session, context.cookies())

    if not isinstance(js, dict):
        return {"ok": False, "error": "response không hợp lệ", "raw": js, "session": session}
    ok, reason = is_bank_bind_success(js)
    return {
        "ok": ok,
        "code": js.get("code"),
        "msg": js.get("msg"),
        "fail_reason": reason or None,
        "raw": js,
        "session": session,
        "method": "playwright",
    }


def bind_bank_for_account(
    account_id: str,
    *,
    bank_id: int = 0,
    bank_name: str = "",
    cardnumber: str = "",
    fund_password: str = "",
    truename: str = "",
    branch: str = "",
    sms_code: str = "",
    use_playwright: bool = False,
    verify: bool = True,
) -> dict[str, Any]:
    session = ensure_session(account_id)

    if not truename:
        ui = session.get("user_info") or {}
        truename = str(ui.get("truename") or "")

    banks_resp = get_bank_list(session)
    if not banks_resp.get("ok"):
        raise RuntimeError(f"Không lấy banklist: {banks_resp.get('msg') or banks_resp.get('raw')}")

    bid = resolve_bank_id(banks_resp.get("banks") or [], bank_id=bank_id, bank_name=bank_name)
    plain = prepare_bankcard_bind_payload(
        bid,
        cardnumber,
        fund_password,
        truename=truename,
        branch=branch,
        sms_code=sms_code,
    )

    from xoso66_proxy import proxy_has_auth, resolve_proxy

    px = resolve_proxy(session)
    if use_playwright and proxy_has_auth(px):
        print("[BANK_BIND] Proxy có user/pass → HTTP, không Playwright", flush=True)
        use_playwright = False

    if use_playwright:
        result = bind_bank_card_playwright(session, plain)
    else:
        result = bind_bank_card(session, plain)
        if (
            not result.get("ok")
            and result.get("code") in (1004, 10058, None)
            and not proxy_has_auth(px)
        ):
            result = bind_bank_card_playwright(session, plain)

    if verify and result.get("ok"):
        info = get_user_info(session)
        refresh = refresh_after_bank_bind(session, use_playwright=use_playwright)
        final_cards = refresh.get("userbanklist_check_order") or refresh.get("userbanklist") or {}
        result["card_count"] = (info.get("data") or {}).get("card_count")
        result["linked_cards"] = final_cards.get("cards")
        result["addresses"] = (refresh.get("useraddresslist") or {}).get("addresses")
        result["refresh"] = refresh
        result["verify_ok"] = bool(final_cards.get("ok"))

    accounts = load_sessions()
    accounts[account_id] = session
    save_sessions(accounts)
    result["account_id"] = account_id
    result["bank_id"] = bid
    result["payload_sent"] = {k: v for k, v in plain.items() if k != "fund_password"}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="XOSO66 liên kết ngân hàng")
    parser.add_argument("-a", "--account", required=True)
    parser.add_argument("--list-banks", action="store_true", help="in danh sách NH")
    parser.add_argument("--list-linked", action="store_true", help="thẻ đã liên kết (userbanklist)")
    parser.add_argument("--list-addresses", action="store_true", help="địa chỉ user (useraddresslist)")
    parser.add_argument(
        "--check-order",
        action="store_true",
        help="userbanklist với check_order=1 (sau bind)",
    )
    parser.add_argument(
        "--refresh-after-bind",
        action="store_true",
        help="chạy 3 request sau bind (userbanklist → useraddresslist → check_order)",
    )
    parser.add_argument("--bank-id", type=int, default=0)
    parser.add_argument("--bank", default="", help="tên NH: ACB, Vietcombank, ...")
    parser.add_argument("--card", dest="cardnumber", default="", help="số tài khoản NH")
    parser.add_argument("--name", default="", help="tên chủ TK (truename)")
    parser.add_argument("--branch", default="", help="chi nhánh (tuỳ chọn)")
    parser.add_argument("--fund-password", default="", help="MK rút 6 số")
    parser.add_argument("--sms-code", default="")
    parser.add_argument("--browser", action="store_true", help="ép Playwright")
    args = parser.parse_args()

    try:
        session = ensure_session(args.account)

        if args.list_banks:
            out = get_bank_list(session)
            banks = out.get("banks") or []
            slim = [{"id": b.get("id"), "name": b.get("name"), "code": b.get("code")} for b in banks]
            print(json.dumps({"ok": out.get("ok"), "count": len(slim), "banks": slim}, indent=2, ensure_ascii=False))
            save_sessions({**load_sessions(), args.account: session})
            return 0 if out.get("ok") else 1

        if args.list_linked:
            params = {"check_order": 1} if args.check_order else {}
            out = get_user_bank_list(session, params=params, use_playwright=args.browser)
            print(json.dumps({"ok": out.get("ok"), "cards": out.get("cards"), "raw": out.get("raw")}, indent=2, ensure_ascii=False))
            save_sessions({**load_sessions(), args.account: session})
            return 0 if out.get("ok") else 1

        if args.refresh_after_bind:
            refresh = refresh_after_bank_bind(session, use_playwright=args.browser)
            slim = {
                "userbanklist_ok": (refresh.get("userbanklist") or {}).get("ok"),
                "useraddresslist_ok": (refresh.get("useraddresslist") or {}).get("ok"),
                "check_order_ok": (refresh.get("userbanklist_check_order") or {}).get("ok"),
                "cards": (refresh.get("userbanklist_check_order") or refresh.get("userbanklist") or {}).get("cards"),
                "addresses": (refresh.get("useraddresslist") or {}).get("addresses"),
            }
            print(json.dumps(slim, indent=2, ensure_ascii=False))
            save_sessions({**load_sessions(), args.account: session})
            return 0 if all(
                (refresh.get(k) or {}).get("ok")
                for k in ("userbanklist", "useraddresslist", "userbanklist_check_order")
            ) else 1

        if args.list_addresses:
            out = get_user_address_list(session, use_playwright=args.browser)
            print(
                json.dumps(
                    {"ok": out.get("ok"), "addresses": out.get("addresses"), "raw": out.get("raw")},
                    indent=2,
                    ensure_ascii=False,
                )
            )
            save_sessions({**load_sessions(), args.account: session})
            return 0 if out.get("ok") else 1

        if not args.cardnumber or not args.fund_password:
            parser.print_help()
            return 1

        result = bind_bank_for_account(
            args.account,
            bank_id=args.bank_id,
            bank_name=args.bank,
            cardnumber=args.cardnumber,
            fund_password=args.fund_password,
            truename=args.name,
            branch=args.branch,
            sms_code=args.sms_code,
            use_playwright=args.browser,
        )
        print(
            json.dumps(
                {
                    "ok": result.get("ok"),
                    "code": result.get("code"),
                    "msg": result.get("msg"),
                    "fail_reason": result.get("fail_reason"),
                    "bank_id": result.get("bank_id"),
                    "card_count": result.get("card_count"),
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
