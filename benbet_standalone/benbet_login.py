# -*- coding: utf-8 -*-
"""
Đăng nhập BEN Bet / benhome1.vip — POST https://api.bencloud.io/user/login

  python benbet_login.py -u USER -p PASS
  python benbet_login.py -u USER -p PASS --proxy host:port:user:pass
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime
from typing import Any

import requests

from benbet_crypto import decrypt_body, encrypt_body

API_BASE = "https://api.bencloud.io"
SITE_ORIGIN = "https://benhome1.vip"
LOGIN_PATH = "/user/login"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


def _parse_proxy(proxy: str | None) -> dict[str, str] | None:
    from benbet_proxy import BenbetProxy

    p = BenbetProxy.from_string(proxy)
    return p.requests_proxies() if p else None


def build_login_payload(
    username: str,
    password: str,
    *,
    keep_pwd: bool = True,
    platform: str | None = None,
    channel: str | None = None,
    aaid: str | None = None,
) -> dict[str, Any]:
    """Payload trước khi bọc {data: encrypt(...)} — khớp form AccountSignIn."""
    body: dict[str, Any] = {
        "platform": platform,
        "channel": channel,
        "aaid": aaid or "",
        "user_name": username.strip(),
        "password": password,
        "keep_pwd": bool(keep_pwd),
    }
    return body


def default_headers(*, lang: str = "vn", token: str = "") -> dict[str, str]:
    h = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "lang": lang,
        "origin": SITE_ORIGIN,
        "user-agent": USER_AGENT,
        "x-date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if token:
        h["lt"] = token
    return h


def login(
    username: str,
    password: str,
    *,
    keep_pwd: bool = True,
    platform: str | None = None,
    channel: str | None = None,
    aaid: str | None = None,
    lang: str = "vn",
    token: str = "",
    proxy: str | None = None,
    session: requests.Session | None = None,
    timeout: float = 30,
) -> dict[str, Any]:
    """
    Đăng nhập. Trả dict:
      ok: bool
      code, message, data — từ API
      lt, user_info — khi thành công (code == 0)
      raw — response đã giải mã
    """
    inner = build_login_payload(
        username,
        password,
        keep_pwd=keep_pwd,
        platform=platform,
        channel=channel,
        aaid=aaid or str(uuid.uuid4()),
    )
    enc = encrypt_body(inner)
    sess = session or requests.Session()
    proxies = _parse_proxy(proxy)

    r = sess.post(
        f"{API_BASE}{LOGIN_PATH}",
        headers=default_headers(lang=lang, token=token),
        json={"data": enc},
        proxies=proxies,
        timeout=timeout,
    )
    r.raise_for_status()

    ct = (r.headers.get("content-type") or "").lower()
    text = (r.text or "").strip()

    if "application/json" in ct:
        try:
            raw = r.json()
        except json.JSONDecodeError:
            raw = {"code": -1, "message": "invalid json", "data": text[:500]}
    elif text:
        try:
            raw = decrypt_body(text)
        except Exception as exc:
            raw = {"code": -1, "message": f"decrypt failed: {exc}", "data": text[:500]}
    else:
        raw = {"code": -1, "message": "empty body", "data": None}

    code = raw.get("code")
    ok = code == 0 or code == "0"
    out: dict[str, Any] = {
        "ok": ok,
        "code": code,
        "message": raw.get("message"),
        "data": raw.get("data"),
        "raw": raw,
        "http_status": r.status_code,
    }
    if ok and isinstance(raw.get("data"), dict):
        data = raw["data"]
        out["lt"] = data.get("lt")
        out["user_info"] = data.get("user_info")
        out["redirect_url"] = data.get("redirect_url")
    return out


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    p = argparse.ArgumentParser(description="Đăng nhập benhome1.vip (api.bencloud.io)")
    p.add_argument("-u", "--username", required=True, help="user_name (4-15 ký tự)")
    p.add_argument("-p", "--password", required=True, help="Mật khẩu")
    p.add_argument("--proxy", help="SOCKS5 host:port hoặc host:port:user:pass")
    p.add_argument("--lang", default="vn")
    p.add_argument("--token", default="", help="Header lt (nếu đã có phiên)")
    p.add_argument("--platform", default=None)
    p.add_argument("--channel", default=None)
    p.add_argument("--aaid", default=None, help="Device id (mặc định random UUID)")
    p.add_argument("--no-keep-pwd", action="store_true")
    p.add_argument("--json", action="store_true", help="In JSON đầy đủ")
    args = p.parse_args(argv)

    try:
        result = login(
            args.username,
            args.password,
            keep_pwd=not args.no_keep_pwd,
            platform=args.platform,
            channel=args.channel,
            aaid=args.aaid,
            lang=args.lang,
            token=args.token,
            proxy=args.proxy,
        )
    except requests.RequestException as exc:
        print(f"Lỗi HTTP: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["ok"]:
        ui = result.get("user_info") or {}
        name = ui.get("user_name") or args.username
        print(f"OK — đăng nhập thành công: {name}")
        if result.get("lt"):
            print(f"lt (token): {result['lt']}")
    else:
        print(f"Thất bại [{result.get('code')}]: {result.get('message')}")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
