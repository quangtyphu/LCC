# -*- coding: utf-8 -*-
"""
C168 — Đăng ký → Mật khẩu rút tiền (qua proxy).

Tạm thời: chưa liên kết bank (sẽ bổ sung sau).

Chạy không tham số → nhập từng mục trong terminal.
"""
from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
from pathlib import Path

from c168_config_util import load_config
from c168_proxy import parse_proxy, proxy_log_label
from c168_register import (
    CDP_DEFAULT_URL,
    RegisterInput,
    manual_watch_session,
    normalize_phone,
    save_account,
)

_DIR = Path(__file__).resolve().parent


def _ask(label: str, *, secret: bool = False, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    while True:
        if secret:
            raw = getpass.getpass(f"{label}{hint}: ")
        else:
            raw = input(f"{label}{hint}: ")
        val = (raw if raw or not default else default).strip()
        if val:
            return val
        print("  → Không được để trống.", file=sys.stderr)


def _prompt_register_fields(args: argparse.Namespace) -> argparse.Namespace:
    need = not all(
        (
            args.username,
            args.password,
            args.phone,
            args.realname,
            args.fund_password,
            args.proxy,
        )
    )
    if not need:
        return args
    print("\n══════════════════════════════════════", file=sys.stderr)
    print("  C168 — ĐĂNG KÝ + MK RÚT (chưa liên kết bank)", file=sys.stderr)
    print("══════════════════════════════════════\n", file=sys.stderr)
    if not args.username:
        args.username = _ask("Tên tài khoản")
    if not args.password:
        args.password = _ask("Mật khẩu đăng nhập", secret=True)
    if not args.phone:
        args.phone = _ask("SĐT (9 số, không 0 đầu, vd: 912345678)")
    if not args.realname:
        args.realname = _ask("Họ tên thật (vd: NGUYEN VAN A)")
    if not args.fund_password:
        args.fund_password = _ask("Mật khẩu rút tiền (6 số)")
    if not args.proxy:
        args.proxy = _ask("Proxy SOCKS5 (host:port:user:pass)")
    print("", file=sys.stderr)
    return args


def _validate_fund_password(value: str) -> str | None:
    s = (value or "").strip()
    if not re.fullmatch(r"\d{6}", s):
        return "fund_password phải đúng 6 chữ số"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="C168: Đăng ký + MK rút (bắt buộc proxy, chưa bind bank)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--username", "-u", default="", help="Tên tài khoản")
    parser.add_argument("--password", "-p", default="", help="Mật khẩu đăng nhập")
    parser.add_argument(
        "--phone",
        default="",
        help="SĐT 9 số, không số 0 đầu (vd: 912345678)",
    )
    parser.add_argument(
        "--realname",
        "-n",
        default="",
        help="Họ tên thật (vd: NGUYEN VAN A)",
    )
    parser.add_argument(
        "--fund-password",
        default="",
        help="Mật khẩu rút tiền 6 số",
    )
    parser.add_argument(
        "--proxy",
        default="",
        help="SOCKS5 host:port:user:pass",
    )
    parser.add_argument(
        "--save",
        default=str(_DIR / "c168_accounts.json"),
        help="File lưu tài khoản sau khi thành công",
    )
    parser.add_argument(
        "--close-browser",
        action="store_true",
        help="Tự đóng Chrome C168 sau khi xong (mặc định: giữ mở)",
    )
    args = parser.parse_args()
    args = _prompt_register_fields(args)

    proxy = args.proxy.strip()
    try:
        parse_proxy(proxy)
    except ValueError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1

    inp = RegisterInput(
        username=args.username.strip(),
        password=args.password,
        phone=args.phone,
        realname=args.realname,
    ).normalized()
    err = inp.validate()
    if err:
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
        return 1

    err = _validate_fund_password(args.fund_password)
    if err:
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
        return 1

    cfg = load_config()
    cfg.setdefault("playwright", {})["headless"] = False

    print(
        f"Đăng ký C168: user={inp.username} phone={inp.phone} "
        f"proxy={proxy_log_label(proxy)}",
        file=sys.stderr,
    )

    out = manual_watch_session(
        cfg=cfg,
        proxy=proxy,
        use_proxy_db=False,
        connect_cdp=CDP_DEFAULT_URL,
        clear_session=True,
        fill_data=inp,
        auto_start_chrome=True,
        auto_submit=True,
        headless_browser=False,
        keep_browser_open=not args.close_browser,
        provision_after_register=True,
        skip_bank_bind=True,
        fund_password=args.fund_password.strip(),
    )

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\nLog: {out.get('log_file')}", file=sys.stderr)

    prov = out.get("provision") or {}
    reg_ok = bool(out.get("ok"))
    fund_ok = bool(prov.get("ok")) if prov else False
    success = reg_ok and fund_ok

    if success:
        rec = {
            "username": inp.username,
            "password": inp.password,
            "phone": inp.phone,
            "realname": inp.realname,
            "fund_password": args.fund_password.strip(),
            "proxy": proxy,
            "mode": "dang_ky_fund_password_only",
        }
        save_account(Path(args.save), rec)
        print(f"Đã lưu {args.save}", file=sys.stderr)
        return 0

    if reg_ok and not fund_ok:
        print(
            "Đăng ký OK nhưng MK rút chưa xong — xem provision trong JSON/log.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
