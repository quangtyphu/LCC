# -*- coding: utf-8 -*-
"""
Giải captcha ảnh XOSO66 (đăng ký) qua Capsolver ImageToTextTask.

Docs: https://docs.capsolver.com/en/guide/captcha/ImageToText/
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import requests

CAPSOLVER_CREATE = "https://api.capsolver.com/createTask"
CAPSOLVER_RESULT = "https://api.capsolver.com/getTaskResult"

_REGISTER_CAPTCHA_CODE = 1011
_WRONG_CAPTCHA_MSG_MARKERS: tuple[str, ...] = (
    "mã xác nhận không chính xác",
    "ma xac nhan khong chinh xac",
)


def load_captcha_config() -> dict[str, Any]:
    """Đọc captcha.* từ config + env XOSO66_CAPTCHA_API_KEY."""
    from xoso66_config_util import load_config

    cfg = load_config()
    cap = dict(cfg.get("captcha") if isinstance(cfg.get("captcha"), dict) else {})
    env_key = (os.environ.get("XOSO66_CAPTCHA_API_KEY") or "").strip()
    if env_key:
        cap["api_key"] = env_key
    cap.setdefault("enabled", True)
    cap.setdefault("provider", "capsolver")
    cap.setdefault("max_attempts", 3)
    cap.setdefault("timeout_sec", 120)
    return cap


def captcha_enabled() -> bool:
    cap = load_captcha_config()
    if not cap.get("enabled", True):
        return False
    return bool(str(cap.get("api_key") or "").strip())


def captcha_base64_from_url(url: str) -> str:
    u = str(url or "").strip()
    if "base64," in u:
        return u.split("base64,", 1)[1].strip()
    return ""


def captcha_base64_from_payload(data: Any) -> str:
    """Trích base64 PNG từ JSON getcaptcha / lỗi register."""
    if isinstance(data, str):
        b64 = captcha_base64_from_url(data)
        if b64:
            return b64
        m = re.search(r"base64,([A-Za-z0-9+/=]+)", data)
        return m.group(1) if m else ""

    if not isinstance(data, dict):
        return ""

    cap = data.get("captcha") if isinstance(data.get("captcha"), dict) else {}
    inner = cap.get("data") if isinstance(cap.get("data"), dict) else cap
    url = str(inner.get("url") or inner.get("image") or cap.get("url") or "")
    b64 = captcha_base64_from_url(url)
    if b64:
        return b64

    nested = data.get("data")
    if isinstance(nested, dict) and nested is not data:
        return captcha_base64_from_payload(nested)
    return ""


def parse_register_error(err_text: str) -> tuple[int | None, str, str]:
    """
    Parse lỗi Vue register — trả (code, msg, captcha_b64).
    """
    raw = str(err_text or "").strip()
    if not raw:
        return None, "", ""

    payload: dict[str, Any] | None = None
    if raw.startswith("{"):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
    elif "{" in raw:
        try:
            payload = json.loads(raw[raw.index("{") :])
        except json.JSONDecodeError:
            payload = None

    if isinstance(payload, dict):
        code = payload.get("code")
        try:
            code_i = int(code) if code is not None else None
        except (TypeError, ValueError):
            code_i = None
        msg = str(payload.get("msg") or "")
        b64 = captcha_base64_from_payload(payload)
        return code_i, msg, b64

    b64 = captcha_base64_from_url(raw)
    if not b64:
        m = re.search(r"base64,([A-Za-z0-9+/=]+)", raw)
        b64 = m.group(1) if m else ""
    return None, raw[:200], b64


def is_wrong_captcha_code(code: int | None) -> bool:
    return code == _REGISTER_CAPTCHA_CODE


def is_wrong_captcha_msg(msg: str) -> bool:
    m = str(msg or "").strip().lower()
    if not m:
        return False
    return any(x in m for x in _WRONG_CAPTCHA_MSG_MARKERS)


def is_wrong_captcha_response(code: int | None = None, msg: str = "") -> bool:
    """code 1011 hoặc msg «Mã xác nhận không chính xác»."""
    try:
        code_i = int(code) if code is not None else None
    except (TypeError, ValueError):
        code_i = None
    return is_wrong_captcha_code(code_i) or is_wrong_captcha_msg(msg)


def solve_image_captcha(
    image_b64: str,
    *,
    api_key: str = "",
    timeout_sec: int = 120,
) -> dict[str, Any]:
    """Capsolver ImageToTextTask — trả {ok, text, ...}."""
    key = str(api_key or "").strip()
    body = str(image_b64 or "").strip()
    if not key:
        return {"ok": False, "error": "Thiếu captcha.api_key (Capsolver)"}
    if not body:
        return {"ok": False, "error": "Thiếu ảnh captcha"}

    task = {"type": "ImageToTextTask", "body": body}
    res = _capsolver_poll(key, task, timeout=int(timeout_sec or 120))
    if not res.get("ok"):
        return res
    text = str((res.get("solution") or {}).get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "Capsolver không trả text", "raw": res}
    return {"ok": True, "text": text, "provider": "capsolver", "raw": res}


def solve_image_captcha_auto(image_b64: str) -> dict[str, Any]:
    cap = load_captcha_config()
    return solve_image_captcha(
        image_b64,
        api_key=str(cap.get("api_key") or ""),
        timeout_sec=int(cap.get("timeout_sec") or 120),
    )


def _capsolver_poll(api_key: str, task: dict[str, Any], *, timeout: int) -> dict[str, Any]:
    try:
        create = requests.post(
            CAPSOLVER_CREATE,
            json={"clientKey": api_key, "task": task},
            timeout=30,
        ).json()
    except Exception as e:
        return {"ok": False, "error": f"Capsolver createTask: {e}"}

    if create.get("errorId"):
        return {
            "ok": False,
            "error": str(create.get("errorDescription") or create.get("errorCode") or create),
        }

    if create.get("status") == "ready":
        return {"ok": True, "solution": create.get("solution") or {}, "task_id": create.get("taskId")}

    task_id = create.get("taskId")
    if not task_id:
        return {"ok": False, "error": f"Capsolver không trả taskId: {create}"}

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        try:
            res = requests.post(
                CAPSOLVER_RESULT,
                json={"clientKey": api_key, "taskId": task_id},
                timeout=30,
            ).json()
        except Exception as e:
            return {"ok": False, "error": f"Capsolver getTaskResult: {e}"}

        if res.get("status") == "ready":
            return {"ok": True, "solution": res.get("solution") or {}, "task_id": task_id}
        if res.get("status") == "failed" or res.get("errorId"):
            return {
                "ok": False,
                "error": str(res.get("errorDescription") or res),
                "raw": res,
            }
    return {"ok": False, "error": "Capsolver timeout", "task_id": task_id}


def solve_cf_anticloudflare(
    session: dict,
    *,
    website_url: str = "",
    html: str = "",
) -> dict[str, Any]:
    """Capsolver AntiCloudflareTask — vượt /__verify/check."""
    from xoso66_cf import BASE_URL
    from xoso66_proxy import ensure_proxy

    cap = load_captcha_config()
    api_key = str(cap.get("api_key") or "").strip()
    if not api_key:
        return {"ok": False, "error": "Thiếu captcha.api_key (Capsolver AntiCloudflare)"}
    ensure_proxy(session)
    proxy = str(session.get("proxy") or "").strip()
    url = str(website_url or f"{BASE_URL}/home/").strip()
    task: dict[str, Any] = {
        "type": "AntiCloudflareTask",
        "websiteURL": url,
        "proxy": proxy,
    }
    ua = str(session.get("user_agent") or "").strip()
    if ua:
        task["userAgent"] = ua
    page_html = str(html or "").strip()
    if page_html:
        task["html"] = page_html[:500_000]

    res = _capsolver_poll(api_key, task, timeout=int(cap.get("cf_timeout_sec") or 180))
    if not res.get("ok"):
        return res

    sol = res.get("solution") or {}
    cookies = sol.get("cookies") if isinstance(sol.get("cookies"), dict) else {}
    from xoso66_session import merge_session_cookies

    # Capsolver CF — không merge PHPSESSID.
    merge_session_cookies(session, cookies, allow_identity=False)

    token = str(sol.get("token") or "").strip()
    if token:
        hdrs = dict(session.get("headers") or {})
        if not token.lower().startswith("bearer"):
            token = f"Bearer.{token}" if not token.startswith("Bearer") else token
        hdrs["cf-auth-token"] = token
        session["headers"] = hdrs

    ok = bool(cookies) or bool(token)
    return {
        "ok": ok,
        "provider": "capsolver",
        "cookies": list(cookies.keys()),
        "has_token": bool(token),
        "raw": res,
    }


FETCH_CAPTCHA_JS = """async () => {
    try {
        const r = await fetch('/server/index/getcaptcha', {
            method: 'GET',
            credentials: 'include',
            headers: { accept: 'application/json' }
        });
        return await r.json();
    } catch (e) {
        return { error: String(e) };
    }
}"""

VUE_STORE_READY_JS = """() => {
    const app = document.querySelector('#app');
    const vm = app && app.__vue__;
    return !!(vm && vm.$store);
}"""

EXTRACT_STORE_TOKENS_JS = """() => {
    const app = document.querySelector('#app');
    const vm = app && app.__vue__;
    const store = vm && vm.$store;
    if (!store) return {};
    return {
        form_token: store.getters.fromToken || '',
        cek_p: (store.state.app && store.state.app.cek_p) || ''
    };
}"""

PAGE_LOAD_DIAG_JS = """() => ({
    url: location.href,
    title: document.title || '',
    hasApp: !!document.querySelector('#app'),
    hasVue: !!(document.querySelector('#app') && document.querySelector('#app').__vue__),
    bodyPreview: (document.body && document.body.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 240)
})"""


def wait_for_vue_store(page, *, timeout_ms: int = 90_000) -> dict[str, Any]:
    """Chờ #app mount Vuex — tránh lỗi null.__vue__ khi CF/proxy chậm."""
    try:
        page.wait_for_function(VUE_STORE_READY_JS, timeout=timeout_ms)
        return {"ok": True}
    except Exception as e:
        diag: dict[str, Any] = {}
        try:
            raw = page.evaluate(PAGE_LOAD_DIAG_JS)
            if isinstance(raw, dict):
                diag = raw
        except Exception:
            pass
        return {"ok": False, "error": str(e), "diag": diag}


def vue_store_unavailable_message(wait: dict[str, Any] | None = None) -> str:
    """Thông báo ngắn khi trang chưa có Vue store."""
    diag = (wait or {}).get("diag") if isinstance((wait or {}).get("diag"), dict) else {}
    url = str(diag.get("url") or "")
    if "/__verify/check" in url:
        return (
            "Cloudflare chặn trang /__verify/check trước khi vào XOSO66. "
            "Đang thử Capsolver AntiCloudflare — nếu vẫn lỗi, đổi proxy."
        )
    parts = [
        "Trang XOSO66 chưa load Vue (#app/__vue__). "
        "Kiểm tra proxy, Cloudflare hoặc thử lại."
    ]
    if diag.get("url"):
        parts.append(f"URL: {diag['url']}")
    if diag.get("title"):
        parts.append(f"title: {diag['title']}")
    if diag.get("bodyPreview"):
        parts.append(f"preview: {diag['bodyPreview']}")
    return " | ".join(parts)


REGISTER_DISPATCH_JS = """async (body) => {
    const app = document.querySelector('#app');
    const vm = app && app.__vue__;
    if (!vm || !vm.$store) return { error: 'no_vue_store' };
    try {
        const r = await vm.$store.dispatch('user/register', body);
        return { ok: true, response: r };
    } catch (e) {
        let detail = '';
        try {
            detail = typeof e === 'object' && e !== null ? JSON.stringify(e) : String(e);
        } catch (err) {
            detail = String(e);
        }
        return {
            ok: false,
            error: detail,
            message: e && e.message ? String(e.message) : detail
        };
    }
}"""


def fetch_captcha_json_via_page(page) -> dict[str, Any]:
    raw = page.evaluate(FETCH_CAPTCHA_JS)
    return raw if isinstance(raw, dict) else {"error": "invalid captcha response"}


def captcha_base64_from_getcaptcha_json(js: dict[str, Any]) -> str:
    data = js.get("data") if isinstance(js.get("data"), dict) else js
    return captcha_base64_from_payload(data if isinstance(data, dict) else js)


def solve_register_captcha_from_page(page) -> dict[str, Any]:
    """GET captcha trên trang → Capsolver → text."""
    cap_js = fetch_captcha_json_via_page(page)
    if cap_js.get("error"):
        return {"ok": False, "error": str(cap_js.get("error")), "raw": cap_js}
    b64 = captcha_base64_from_getcaptcha_json(cap_js)
    if not b64:
        return {"ok": False, "error": "Không lấy được ảnh captcha", "raw": cap_js}
    solved = solve_image_captcha_auto(b64)
    if solved.get("ok"):
        solved["captcha_b64_len"] = len(b64)
    return solved
