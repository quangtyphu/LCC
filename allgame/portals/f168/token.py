# -*- coding: utf-8 -*-
"""F168 — đọc / kiểm tra token lobby qua hall API."""

from __future__ import annotations

import json
import time
from typing import Any
from urllib import error, request

PORTAL_ID = "f168"
_DEFAULT_HALL_ORIGIN = "https://ah861f.f1sau8.com"
_LOGOUT_PATH = "/hall/api/gameCenter/gameApi/logout"


def _first_non_empty(src: dict[str, Any], keys: tuple[str, ...], default: str = "") -> str:
    for key in keys:
        val = src.get(key)
        if val is None:
            continue
        out = str(val).strip()
        if out:
            return out
    return default


def _safe_int(value: Any, fallback: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def _normalize_origin(raw: str) -> str:
    val = str(raw or "").strip().rstrip("/")
    if not val:
        return _DEFAULT_HALL_ORIGIN
    if val.startswith("http://") or val.startswith("https://"):
        return val
    return f"https://{val}"


class F168TokenChecker:
    portal_id = PORTAL_ID

    def _build_context(self, account: dict[str, Any]) -> dict[str, Any]:
        session = account.get("session_json") if isinstance(account.get("session_json"), dict) else {}
        extra = (
            account.get("portal_extra_json")
            if isinstance(account.get("portal_extra_json"), dict)
            else {}
        )
        ctx = {**extra, **session}
        now = int(time.time())

        hall_origin = _normalize_origin(
            _first_non_empty(
                ctx,
                ("hall_api", "api_origin", "hall_origin", "hall", "apiBase", "api_base"),
                _DEFAULT_HALL_ORIGIN,
            )
        )
        site_domain = _first_non_empty(
            ctx,
            ("domain", "site_domain", "site", "site_host", "web_domain"),
            "f1686s.com",
        )
        web_origin = _normalize_origin(
            _first_non_empty(
                ctx,
                ("origin", "web_origin", "referer_origin", "site_origin"),
                f"https://{site_domain}",
            )
        )
        app_version = _first_non_empty(ctx, ("appversion", "x-version", "version"), "v7.3.17")
        x_version = _first_non_empty(ctx, ("x-version", "x_version"), app_version.lstrip("v"))
        token = _first_non_empty(ctx, ("session_key", "token", "sessionKey", "access_token"))
        newjwt = _first_non_empty(ctx, ("newjwt", "jwt_token", "jwt", "jwtToken"))
        device = _first_non_empty(ctx, ("device", "device_uuid", "uuid"))
        browserfingerid = _first_non_empty(
            ctx, ("browserfingerid", "browser_finger_id", "fingerprint")
        )
        x_device = _first_non_empty(ctx, ("x-device", "x_device"), "1-1")
        sitecode = _first_non_empty(ctx, ("sitecode", "site_code"), "280")
        user_agent = _first_non_empty(
            ctx,
            ("user-agent", "user_agent"),
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        )
        x_object_id = ctx.get("x-object-id") or ctx.get("x_object_id")
        if not x_object_id:
            uid = account.get("username") or account.get("uid") or ctx.get("uid") or ""
            x_object_id = json.dumps(
                {
                    "uid": str(uid),
                    "browserLanguage": "vi-VN",
                    "init": {
                        "device": device,
                        "created": now * 1000,
                        "version": max(0, (now - 3600) * 1000),
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )

        body_time = _safe_int(ctx.get("timestamp"), now)
        body = {
            "os_type": 3,
            "callContext": json.dumps(
                {
                    "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(body_time)),
                    "scene": "allgame balance check",
                    "stack": ["F168TokenChecker.test_token (allgame)"],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "time": body_time,
        }
        headers: dict[str, str] = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "vi",
            "appsystem": "Windows 10",
            "appversion": app_version,
            "browsertype": "Chrome v148.0.0.0",
            "clienttimezone": "+7",
            "content-type": "application/json",
            "currency": "VND",
            "devicebrand": "unknown",
            "devicemodel": "Chrome v148.0.0.0",
            "domain": site_domain,
            "language": "vi",
            "operatingsystem": "Windows",
            "origin": web_origin,
            "physicaldevicemodel": "unknown",
            "platformtype": "5",
            "referer": f"{web_origin}/",
            "sitecode": sitecode,
            "timestamp": str(body_time),
            "user-agent": user_agent,
            "webauthndomain": site_domain,
            "x-custom-referer": f"{web_origin}/",
            "x-data-mode": "plain",
            "x-device": x_device,
            "x-object-id": str(x_object_id),
            "x-version": x_version,
        }
        if token:
            headers["token"] = token
        if newjwt:
            headers["newjwt"] = newjwt
        if device:
            headers["device"] = device
        if browserfingerid:
            headers["browserfingerid"] = browserfingerid
        return {
            "hall_origin": hall_origin,
            "logout_url": f"{hall_origin}{_LOGOUT_PATH}",
            "headers": headers,
            "body": body,
            "username": account.get("username"),
        }

    def _check_by_logout_api(self, account: dict[str, Any]) -> dict[str, Any]:
        ctx = self._build_context(account)
        payload = json.dumps(ctx["body"], ensure_ascii=False).encode("utf-8")
        req = request.Request(
            str(ctx["logout_url"]),
            data=payload,
            headers={k: str(v) for k, v in dict(ctx["headers"]).items()},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=20) as resp:
                status = int(getattr(resp, "status", 0) or 0)
                raw = resp.read().decode("utf-8", errors="replace")
        except error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            return {
                "ok": False,
                "status": int(e.code or 0),
                "error": f"http_{int(e.code or 0)}",
                "response_text": body[:500],
                "request": ctx,
            }
        except Exception as e:
            return {
                "ok": False,
                "status": 0,
                "error": str(e),
                "request": ctx,
            }

        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"raw": raw}
        code = data.get("code") if isinstance(data, dict) else None
        ok = status == 200 and (code in (1, "1") or data.get("success") is True)
        balance = None
        if isinstance(data, dict):
            dd = data.get("data")
            if isinstance(dd, dict):
                for key in (
                    "game_gold",
                    "totalGold",
                    "balance",
                    "money",
                    "wallet",
                    "credit",
                    "amount",
                ):
                    if dd.get(key) is not None:
                        balance = dd.get(key)
                        break
        return {
            "ok": bool(ok),
            "status": status,
            "code": code,
            "balance": balance,
            "response": data,
            "request": ctx,
        }

    def test_token(self, account: dict[str, Any]) -> bool:
        out = self._check_by_logout_api(account)
        return bool(out.get("ok"))

    def read_token_snapshot(self, account: dict[str, Any]) -> dict[str, Any]:
        out = self._check_by_logout_api(account)
        req = out.get("request") if isinstance(out.get("request"), dict) else {}
        headers = req.get("headers") if isinstance(req.get("headers"), dict) else {}
        return {
            "portal_id": self.portal_id,
            "username": account.get("username"),
            "implemented": True,
            "has_token": bool(headers.get("token")),
            "has_newjwt": bool(headers.get("newjwt")),
            "status": out.get("status"),
            "code": out.get("code"),
            "balance": out.get("balance"),
            "ok": out.get("ok", False),
            "logout_url": req.get("logout_url"),
            "error": out.get("error"),
        }

    def refresh_token(self, account: dict[str, Any]) -> dict[str, Any]:
        out = self._check_by_logout_api(account)
        return {
            "ok": bool(out.get("ok")),
            "portal_id": self.portal_id,
            "username": account.get("username"),
            "status": out.get("status"),
            "code": out.get("code"),
            "balance": out.get("balance"),
            "error": out.get("error"),
            "logout_url": (
                out.get("request", {}).get("logout_url")
                if isinstance(out.get("request"), dict)
                else None
            ),
        }
