# -*- coding: utf-8 -*-
"""
Đăng ký C168 — file hoàn chỉnh.

Form (tab ĐĂNG KÝ tại /home/register):
  1. Tên tài khoản (username)
  2. Mật khẩu (password)
  3. Số điện thoại (+84, không gõ số 0 đầu)
  4. Họ và tên thật (realname)
  → Bấm ĐĂNG KÝ → Cloudflare Turnstile tự xác minh (giống đăng ký tay)
  → Tùy chọn: Capsolver nếu `captcha.mode` = capsolver

Cài đặt:
  pip install playwright requests
  playwright install chromium
  copy c168_config.example.json c168_config.json

Chạy:
  python c168_register.py --username user01 --password Abc123456 --phone 912345678 --realname "NGUYEN VAN A"
  python c168_register.py --random
  python c168_register.py --random --headed
  python c168_register.py --manual --headed   # chỉ mở web, bạn tự đăng ký, script ghi log API
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from c168_captcha_solver import (
    inject_geetest_solution,
    inject_turnstile_token,
    solve_captcha,
    solve_turnstile,
)
from c168_config_util import load_config
from c168_provision_ui import (
    PROVISION_API_SUFFIXES,
    random_bank_account,
    random_fund_password,
    run_post_register_provision,
)
from c168_proxy import (
    chrome_proxy_server,
    list_proxies_prioritized_from_lc79_db,
    playwright_proxy_dict,
    proxy_log_label,
)

_DIR = Path(__file__).resolve().parent
REGISTER_API_SUFFIX = "/hall/api/member/register"
REGISTER_URL_PATH = "/home/register"
CDP_DEBUG_PORT = 9333
CDP_DEFAULT_URL = f"http://127.0.0.1:{CDP_DEBUG_PORT}"
C168_CHROME_PROFILE_NAME = "c168-chrome-profile"

# Placeholder trên form (tiếng Việt)
PH_USERNAME = re.compile(r"tên tài khoản", re.I)
PH_PASSWORD = re.compile(r"mật khẩu", re.I)
PH_PHONE = re.compile(r"nhập sđt|số điện thoại|sđt", re.I)
PH_REALNAME = re.compile(r"họ và tên", re.I)


@dataclass
class RegisterInput:
    """4 trường bắt buộc trên form đăng ký."""

    username: str
    password: str
    phone: str
    realname: str

    def normalized(self) -> RegisterInput:
        u = str(self.username or "").strip()
        p = str(self.password or "").strip()
        phone = normalize_phone(self.phone)
        name = str(self.realname or "").strip() or "NGUYEN VAN A"
        return RegisterInput(username=u, password=p, phone=phone, realname=name)

    def validate(self) -> str | None:
        n = self.normalized()
        if len(n.username) < 4:
            return "username quá ngắn (tối thiểu 4 ký tự)"
        if len(n.password) < 6:
            return "password quá ngắn (tối thiểu 6 ký tự)"
        if not re.fullmatch(r"[35789]\d{8}", n.phone):
            return "phone phải 9 chữ số (không 0 đầu), bắt đầu 3/5/7/8/9 (vd: 912345678, 585181806)"
        if len(n.realname) < 3:
            return "realname quá ngắn"
        return None


def normalize_phone(phone: str) -> str:
    """+84 / 09xx → 9xxxxxxxx (ô SĐT trên form không nhập số 0 đầu)."""
    s = re.sub(r"\D", "", str(phone or "").strip())
    if s.startswith("84"):
        s = s[2:]
    if s.startswith("0"):
        s = s[1:]
    return s


def random_username(prefix: str = "qc") -> str:
    return prefix + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


def random_password() -> str:
    return "Abc" + "".join(random.choices(string.digits, k=6))


def random_phone_vn() -> str:
    return "9" + "".join(random.choices(string.digits, k=8))


def random_realname() -> str:
    ho = random.choice(["NGUYEN", "TRAN", "LE", "PHAM", "HOANG"])
    ten = random.choice(["VAN A", "THI B", "MINH C", "DUC D"])
    return f"{ho} {ten}"


def _pause(page, ms: int) -> None:
    if ms > 0:
        page.wait_for_timeout(ms)


def _apply_mobile_viewport(page) -> None:
    """Viewport mobile (đăng ký / flow cũ)."""
    try:
        page.set_viewport_size({"width": 390, "height": 844})
    except Exception:
        pass
    try:
        page.evaluate(
            """() => {
              let m = document.querySelector('meta[name=viewport]');
              if (!m) {
                m = document.createElement('meta');
                m.name = 'viewport';
                document.head.appendChild(m);
              }
              m.content = 'width=device-width, initial-scale=1, maximum-scale=1';
            }"""
        )
    except Exception:
        pass
    page.wait_for_timeout(800)


def _apply_desktop_viewport(page, *, width: int = 1440, height: int = 900) -> None:
    """Giao diện PC — dùng cho login / mở game vendor."""
    try:
        page.set_viewport_size({"width": width, "height": height})
    except Exception:
        pass
    try:
        cdp = page.context.new_cdp_session(page)
        cdp.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": width,
                "height": height,
                "deviceScaleFactor": 1,
                "mobile": False,
                "screenWidth": width,
                "screenHeight": height,
            },
        )
        cdp.detach()
    except Exception:
        pass
    try:
        page.evaluate(
            f"""() => {{
              let m = document.querySelector('meta[name=viewport]');
              if (!m) {{
                m = document.createElement('meta');
                m.name = 'viewport';
                document.head.appendChild(m);
              }}
              m.content = 'width={width}, initial-scale=1';
            }}"""
        )
    except Exception:
        pass
    page.wait_for_timeout(400)


def _safe_fill_input(loc, value: str, *, char_delay_ms: int = 0) -> None:
    """fill(force) / JS — tránh cờ +84 che click (pointer intercept)."""
    loc.scroll_into_view_if_needed(timeout=8000)
    try:
        loc.fill(value, timeout=12_000, force=True)
        return
    except Exception:
        pass
    try:
        loc.evaluate(
            """(el, v) => {
              el.focus();
              el.value = v;
              el.dispatchEvent(new Event('input', { bubbles: true }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            value,
        )
        return
    except Exception:
        pass
    if char_delay_ms > 0:
        loc.press_sequentially(value, delay=char_delay_ms, timeout=15_000)
    else:
        loc.fill(value, timeout=12_000, force=True)


def _human_type(loc, value: str, *, char_delay_ms: int = 0) -> None:
    _safe_fill_input(loc, value, char_delay_ms=char_delay_ms)


def _fill_field(page, selector: str, value: str, *, char_delay_ms: int = 0) -> bool:
    loc = page.locator(selector).first
    if not loc.count():
        return False
    try:
        _human_type(loc, value, char_delay_ms=char_delay_ms)
        return True
    except Exception:
        return False


def _fill_c168_form_by_name(
    page,
    data: RegisterInput,
    *,
    char_delay_ms: int = 0,
    pause_between_ms: int = 0,
) -> dict[str, bool]:
    """Điền theo data-input-name / section form C168 (tránh overlap cờ VN)."""
    filled = {"username": False, "password": False, "phone": False, "realname": False}
    fields: list[tuple[str, str, list[str]]] = [
        (
            "username",
            data.username,
            [
                'input[data-input-name="account"]',
                'section[data-item-name="account"] input.ui-input__input',
            ],
        ),
        (
            "password",
            data.password,
            [
                'input[data-input-name="userpass"]',
                'input[autocomplete="new-password"]',
            ],
        ),
        (
            "phone",
            data.phone,
            [
                'section[data-item-name="phone"] input.ui-input__input',
                'section[data-item-name="phone"] input',
            ],
        ),
        (
            "realname",
            data.realname,
            [
                'section[data-item-name="realName"] input',
                'section[data-item-name="realname"] input',
            ],
        ),
    ]
    for key, val, selectors in fields:
        for sel in selectors:
            loc = page.locator(sel).first
            if not loc.count():
                continue
            try:
                _safe_fill_input(loc, val, char_delay_ms=char_delay_ms)
                filled[key] = True
                _pause(page, pause_between_ms)
                break
            except Exception:
                continue
    return filled


def _fill_by_placeholders(
    page,
    data: RegisterInput,
    *,
    char_delay_ms: int = 0,
    pause_between_ms: int = 0,
) -> dict[str, bool]:
    """Điền 4 ô: Tên TK → Mật khẩu → SĐT → Họ tên (theo form C168)."""
    filled = _fill_c168_form_by_name(
        page, data, char_delay_ms=char_delay_ms, pause_between_ms=pause_between_ms
    )

    for i in range(page.locator("input:visible, textarea:visible").count()):
        if all(filled.values()):
            break
        el = page.locator("input:visible, textarea:visible").nth(i)
        try:
            ph = (el.get_attribute("placeholder") or "").strip()
            typ = (el.get_attribute("type") or "text").lower()
        except Exception:
            continue
        if PH_USERNAME.search(ph) and not filled["username"]:
            _safe_fill_input(el, data.username, char_delay_ms=char_delay_ms)
            filled["username"] = True
            _pause(page, pause_between_ms)
        elif (PH_PASSWORD.search(ph) or typ == "password") and not filled["password"]:
            _safe_fill_input(el, data.password, char_delay_ms=char_delay_ms)
            filled["password"] = True
            _pause(page, pause_between_ms)
        elif (PH_PHONE.search(ph) or typ == "tel") and not filled["phone"]:
            _safe_fill_input(el, data.phone, char_delay_ms=char_delay_ms)
            filled["phone"] = True
            _pause(page, pause_between_ms)
        elif PH_REALNAME.search(ph) and not filled["realname"]:
            _safe_fill_input(el, data.realname, char_delay_ms=char_delay_ms)
            filled["realname"] = True
            _pause(page, pause_between_ms)

    # Fallback đúng thứ tự trên UI (ảnh form)
    if not filled["username"]:
        texts = page.locator("input[type='text']:visible")
        if texts.count() >= 1:
            _human_type(texts.nth(0), data.username, char_delay_ms=char_delay_ms)
            filled["username"] = True
            _pause(page, pause_between_ms)
    if not filled["password"]:
        pw = page.locator("input[type='password']:visible")
        if pw.count():
            _human_type(pw.first, data.password, char_delay_ms=char_delay_ms)
            filled["password"] = True
        elif page.locator("input[type='text']:visible").count() >= 2:
            _human_type(
                page.locator("input[type='text']:visible").nth(1),
                data.password,
                char_delay_ms=char_delay_ms,
            )
            filled["password"] = True
        _pause(page, pause_between_ms)
    if not filled["phone"]:
        filled["phone"] = _fill_field(
            page, "input[type='tel']:visible", data.phone, char_delay_ms=char_delay_ms
        )
        _pause(page, pause_between_ms)
    if not filled["realname"]:
        texts = page.locator("input[type='text']:visible")
        if texts.count() >= 2:
            _human_type(texts.last, data.realname, char_delay_ms=char_delay_ms)
            filled["realname"] = True

    return filled


def _ensure_register_tab(page) -> None:
    try:
        tab = page.get_by_text("ĐĂNG KÝ", exact=False).last
        if tab.count() and tab.is_visible():
            tab.click(timeout=5000)
            page.wait_for_timeout(800)
    except Exception:
        pass


def _accept_terms(page) -> None:
    try:
        page.locator("input[type='checkbox']:visible").first.check(force=True, timeout=3000)
    except Exception:
        pass


def _dismiss_blocking_popups(page) -> None:
    """Đóng popup khuyến mãi / thông báo che nút ĐĂNG KÝ."""
    for label in ("Huỷ Bỏ", "Hủy Bỏ", "Đóng", "Bỏ qua", "Cancel"):
        try:
            btn = page.get_by_role("button", name=label)
            if btn.count():
                btn.first.click(force=True, timeout=2500)
                page.wait_for_timeout(400)
                return
        except Exception:
            continue
    try:
        page.locator("button:has-text('Huỷ'), button:has-text('Hủy')").first.click(
            force=True, timeout=2000
        )
        page.wait_for_timeout(400)
    except Exception:
        pass


def _bind_manual_page_listeners(page, on_request, on_response) -> None:
    page.on("request", on_request)
    page.on("response", on_response)


def _bind_manual_cdp_browser(browser, on_request, on_response) -> None:
    """Gắn listener mọi tab/context — tránh bỏ sót API khi đổi tab."""
    for ctx in browser.contexts:
        def _on_new_page(p):
            _bind_manual_page_listeners(p, on_request, on_response)

        ctx.on("page", _on_new_page)
        for p in ctx.pages:
            _bind_manual_page_listeners(p, on_request, on_response)


def _c168_chrome_profile_dir() -> str:
    return os.path.join(os.environ.get("TEMP", "."), C168_CHROME_PROFILE_NAME)


def _kill_c168_chrome_only() -> None:
    """Chỉ tắt Chrome C168 (profile c168-chrome-profile), không đụng Chrome cá nhân."""
    marker = C168_CHROME_PROFILE_NAME
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.CommandLine -like '*{marker}*' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        timeout=20,
    )
    time.sleep(0.8)


def _wipe_c168_chrome_profile(*, kill_chrome: bool = True) -> None:
    """Xóa profile C168 riêng — không tắt Chrome đang dùng hằng ngày."""
    if kill_chrome:
        _kill_c168_chrome_only()
    profile = _c168_chrome_profile_dir()
    if os.path.isdir(profile):
        shutil.rmtree(profile, ignore_errors=True)


def _page_needs_register(page, reg_path: str) -> bool:
    url = (page.url or "").lower()
    if reg_path.lower() in url:
        return True
    if "/login" in url:
        return False
    try:
        text = page.evaluate(
            "() => (document.body && document.body.innerText) || ''"
        )
        low = str(text).lower()
        if any(
            x in low
            for x in (
                "đăng xuất",
                "dang xuat",
                "logout",
                "số dư",
                "so du",
                "nạp tiền",
                "nap tien",
            )
        ):
            return False
    except Exception:
        pass
    return reg_path not in url


def _clear_c168_browser_session(
    browser: Any,
    context: Any,
    page: Any,
    *,
    base: str,
    reg_path: str,
) -> None:
    """Xóa cookie/storage và mở lại trang đăng ký."""
    reg_url = base.rstrip("/") + reg_path
    contexts = list(browser.contexts) if browser else [context]
    for ctx in contexts:
        try:
            ctx.clear_cookies()
        except Exception:
            pass
        for p in ctx.pages:
            try:
                p.evaluate(
                    """async () => {
                      try { localStorage.clear(); sessionStorage.clear(); } catch (e) {}
                      try {
                        if ('caches' in window) {
                          const ks = await caches.keys();
                          await Promise.all(ks.map(k => caches.delete(k)));
                        }
                      } catch (e) {}
                    }"""
                )
            except Exception:
                pass
    for _ in range(2):
        try:
            page.goto(reg_url, wait_until="domcontentloaded", timeout=120_000)
            page.wait_for_timeout(1500)
        except Exception:
            pass
        if _page_needs_register(page, reg_path):
            break
        for ctx in contexts:
            try:
                ctx.clear_cookies()
            except Exception:
                pass


def _finalize_manual_result(
    register_hits: list[dict[str, Any]],
    api_events: list[dict[str, Any]],
    result: dict[str, Any],
) -> None:
    for hit in register_hits:
        try:
            j = json.loads(hit.get("body") or "{}")
            if j.get("code") == 1:
                result["ok"] = True
        except Exception:
            continue
    if register_hits:
        hit = register_hits[-1]
        try:
            j = json.loads(hit.get("body") or "{}")
            result["last_register_code"] = j.get("code")
            result["last_register_msg"] = j.get("msg")
            result["register_response"] = hit.get("body")
        except Exception:
            pass
    result["api_event_count"] = len(api_events)
    result["register_calls"] = [
        {
            "status": h.get("status"),
            "body_preview": (h.get("body") or "")[:500],
        }
        for h in register_hits
    ]


def _close_browser(
    browser,
    page,
    *,
    keep_open_ms: int,
    headless: bool,
    kill_chrome: bool = False,
) -> None:
    if keep_open_ms < 0:
        print(
            "\nGiữ Chrome C168 mở — tắt cửa sổ khi bạn xong (Chrome cá nhân không bị đụng).",
            file=sys.stderr,
        )
        return
    if keep_open_ms > 0 and page is not None:
        print(
            f"\nGiữ cửa sổ C168 {keep_open_ms // 1000}s…",
            file=sys.stderr,
        )
        try:
            page.wait_for_timeout(keep_open_ms)
        except Exception:
            pass
    try:
        if browser is not None:
            browser.close()
    except Exception:
        pass
    if kill_chrome:
        _kill_c168_chrome_only()
        print("Đã đóng Chrome C168 (profile riêng).", file=sys.stderr)


def _proxy_candidates(
    *,
    explicit: str,
    use_db: bool,
    cfg: dict[str, Any],
    max_tries: int = 20,
) -> list[str]:
    if explicit.strip():
        return [explicit.strip()]
    if not use_db:
        return [""]
    proxy_cfg = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else {}
    if proxy_cfg.get("enabled") is False:
        return [""]
    db_path = proxy_cfg.get("db_path")
    pool = list_proxies_prioritized_from_lc79_db(db_path)
    limit = max(1, int(max_tries or proxy_cfg.get("max_tries") or 20))
    return pool[:limit] if pool else [""]


def _is_bot_block_error(msg: str) -> bool:
    low = (msg or "").lower()
    return (
        "chặn bot" in low
        or "robot" in low
        or "phát hiện hành vi" in low
        or "bất thường" in low
    )


def _detect_bot_block(page) -> str | None:
    try:
        text = page.evaluate(
            "() => (document.body && document.body.innerText) || ''"
        )
        low = str(text).lower()
        if "phát hiện hành vi" in low or "hành vi bất thường của robot" in low:
            return (
                "Site chặn bot (popup Mẹo). Đã dùng proxy SOCKS5 từ DB — "
                "thử proxy khác, --headed, hoặc chạy lại sau."
            )
    except Exception:
        pass
    return None


def _click_register_submit(page) -> None:
    for sel in (
        "#insideRegisterSubmitClick",
        "button:has-text('ĐĂNG KÝ')",
        "button.ui-button--primary",
    ):
        loc = page.locator(sel)
        if sel.startswith("button:has-text"):
            loc = page.get_by_role("button", name="ĐĂNG KÝ").last
        if loc.count():
            loc.last.click(force=True, timeout=20_000)
            return
    raise RuntimeError("Không tìm thấy nút ĐĂNG KÝ")


def _turnstile_snapshot(page) -> dict[str, Any]:
    return page.evaluate(
        """() => {
          const r = document.querySelector('[name="cf-turnstile-response"]');
          const val = r ? (r.value || '') : '';
          const text = document.body ? document.body.innerText : '';
          const verifying = /Đang xác minh|Verifying|Success/i.test(text);
          const iframe = !!document.querySelector(
            'iframe[src*="challenges.cloudflare"], iframe[src*="turnstile"]'
          );
          return {
            has_response: val.length > 30,
            response_len: val.length,
            verifying,
            has_iframe: iframe,
          };
        }"""
    )


def _parse_register_json(body: str) -> dict[str, Any]:
    try:
        return json.loads(body or "{}")
    except Exception:
        return {}


def _is_encrypted_register_success(body: str, status: int) -> bool:
    """API 200 + body dài dạng chipher (không phải JSON 1134) — thường là đăng ký OK."""
    raw = (body or "").strip()
    if status != 200 or len(raw) < 40:
        return False
    if "1134" in raw or "errorCode" in raw:
        return False
    try:
        json.loads(raw)
        return False
    except Exception:
        pass
    sample = re.sub(r"\s+", "", raw[:400])
    return bool(re.fullmatch(r"[A-Za-z0-9+/=_-]+", sample))


def _ensure_logged_in_after_register(page, base: str) -> None:
    """API register OK nhưng URL vẫn /register — đóng popup và vào trang chủ."""
    reg_url = base.rstrip("/") + REGISTER_URL_PATH
    for _ in range(40):
        url = (page.url or "").lower()
        if "/register" not in url and "/login" not in url:
            return
        _dismiss_blocking_popups(page)
        for label in (
            "Vào trang chủ",
            "Trang chủ",
            "Bắt đầu chơi",
            "Nạp ngay",
            "Xác nhận",
            "Hoàn tất",
            "OK",
            "Đăng nhập",
        ):
            try:
                loc = page.get_by_text(label, exact=False)
                if loc.count() and loc.first.is_visible():
                    loc.first.click(force=True, timeout=3000)
                    page.wait_for_timeout(1200)
                    break
            except Exception:
                continue
        if "/register" in (page.url or "").lower():
            try:
                page.goto(
                    base.rstrip("/") + "/home",
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
            except Exception:
                pass
        page.wait_for_timeout(800)
    if "/register" in (page.url or "").lower():
        try:
            page.goto(reg_url.replace("/register", "/home"), timeout=60_000)
        except Exception:
            page.goto(base.rstrip("/") + "/", timeout=60_000)


def _detect_register_ui_success(page, reg_path: str) -> bool:
    """Web báo thành công (toast / text) dù API log không parse được."""
    try:
        text = page.evaluate(
            "() => (document.body && document.body.innerText) || ''"
        ) or ""
        low = text.lower()
        if any(
            x in low
            for x in (
                "đăng ký thành công",
                "dang ky thanh cong",
                "đăng ký thành công!",
                "chúc mừng bạn đã đăng ký",
                "chuc mung ban da dang ky",
            )
        ):
            return True
        for sel in ('[class*="toast"]', '[class*="success"]', '[class*="dialog"]'):
            loc = page.locator(sel)
            for i in range(min(loc.count(), 8)):
                try:
                    t = (loc.nth(i).inner_text(timeout=400) or "").lower()
                except Exception:
                    continue
                if "thành công" in t and "đăng ký" in t:
                    return True
        url = (page.url or "").lower()
        if "/login" in url and "/register" not in url:
            return True
        if "/register" not in url and "/login" in url:
            if "thành công" in low or "success" in low:
                return True
    except Exception:
        pass
    return False


def _infer_register_success(
    page,
    register_hits: list[dict[str, Any]],
    *,
    reg_path: str,
    body_j: dict[str, Any],
    wait_ok: bool,
) -> tuple[bool, str]:
    if wait_ok or body_j.get("code") == 1:
        return True, "api_code_1"
    for ev in reversed(register_hits):
        body = str(ev.get("body") or "")
        status = int(ev.get("status") or 0)
        j = _parse_register_json(body)
        if j.get("code") == 1:
            return True, "api_code_1"
        if j.get("code") == 1134:
            continue
        if _is_encrypted_register_success(body, status):
            return True, "api_encrypted_200"
    if _detect_register_ui_success(page, reg_path):
        return True, "ui_success"
    return False, ""


def _best_register_from_events(events: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Ưu tiên response code==1; không thì lấy lần gọi API cuối."""
    last_ev: dict[str, Any] | None = None
    for ev in events:
        last_ev = ev
        body = str(ev.get("body") or "")
        j = _parse_register_json(body)
        if j.get("code") == 1 or j.get("success") is True:
            return j, ev
        if _is_encrypted_register_success(body, int(ev.get("status") or 0)):
            return {"code": 1, "success": True, "encrypted": True}, ev
    if last_ev:
        return _parse_register_json(str(last_ev.get("body") or "")), last_ev
    return {}, None


def _wait_turnstile_ready(page, timeout_ms: int = 60_000) -> dict[str, Any]:
    """Chờ widget Turnstile có token (đăng ký tay thường tự chạy trước khi bấm nút)."""
    deadline = time.time() + timeout_ms / 1000
    last: dict[str, Any] = {}
    while time.time() < deadline:
        last = _turnstile_snapshot(page)
        if last.get("has_response"):
            return last
        page.wait_for_timeout(500)
    return last


def _wait_after_register_click(
    page,
    register_events: list[dict[str, Any]],
    *,
    from_index: int,
    post_click_wait_ms: int,
    flow_wait_ms: int,
) -> dict[str, Any]:
    """Chờ captcha (~5s) và chuyển trang đăng nhập trước khi đọc API."""
    sec = max(1, post_click_wait_ms // 1000)
    print(
        f"Đợi {sec}s sau bấm ĐĂNG KÝ (captcha load / chuyển trang)…",
        file=sys.stderr,
    )
    page.wait_for_timeout(post_click_wait_ms)
    slice_ev = register_events[from_index:]
    body_j, ev = _best_register_from_events(slice_ev)
    code = body_j.get("code")
    enc = body_j.get("encrypted")
    url = page.url or ""
    print(
        f"  Sau {sec}s: API code={code!r} encrypted={bool(enc)} url={url[:80]}",
        file=sys.stderr,
    )
    if body_j.get("code") == 1 or enc:
        return {
            "ok": True,
            "body_j": body_j,
            "event": ev,
            "turnstile": _turnstile_snapshot(page),
        }
    if _detect_register_ui_success(page, REGISTER_URL_PATH):
        return {
            "ok": True,
            "body_j": {"code": 1, "ui": True},
            "event": ev,
            "turnstile": _turnstile_snapshot(page),
        }
    return _wait_register_flow(
        page, register_events, from_index=from_index, timeout_ms=flow_wait_ms
    )


def _resubmit_register_until_ok(
    page,
    register_events: list[dict[str, Any]],
    *,
    max_attempts: int = 4,
    turnstile_wait_ms: int = 30_000,
    register_wait_ms: int = 45_000,
) -> dict[str, Any]:
    """Sau 1134: chờ Turnstile trên Chrome thật rồi bấm ĐĂNG KÝ lại."""
    last_wait: dict[str, Any] = {}
    for n in range(1, max_attempts + 1):
        print(
            f"Captcha 1134 — chờ Turnstile, bấm ĐĂNG KÝ lại ({n}/{max_attempts})…",
            file=sys.stderr,
        )
        ts = _wait_turnstile_ready(page, turnstile_wait_ms)
        _dismiss_blocking_popups(page)
        if ts.get("has_response"):
            print("  Turnstile đã có token.", file=sys.stderr)
        idx = len(register_events)
        try:
            _click_register_submit(page)
        except Exception as e:
            print(f"  Không bấm ĐĂNG KÝ: {e}", file=sys.stderr)
            page.wait_for_timeout(1500)
            continue
        last_wait = _wait_after_register_click(
            page,
            register_events,
            from_index=idx,
            post_click_wait_ms=6000,
            flow_wait_ms=register_wait_ms,
        )
        body_j = last_wait.get("body_j") or {}
        if last_wait.get("ok") or body_j.get("code") == 1:
            return last_wait
        if body_j.get("code") not in (1134, None, 0):
            return last_wait
    return last_wait


def _wait_register_flow(
    page,
    register_events: list[dict[str, Any]],
    *,
    from_index: int,
    timeout_ms: int,
) -> dict[str, Any]:
    """
    Sau khi bấm ĐĂNG KÝ: chờ Turnstile (Đang xác minh...) và/hoặc API register.
    Giống đăng ký tay — một lần bấm, widget tự chạy.
    """
    deadline = time.time() + timeout_ms / 1000
    last_turnstile: dict[str, Any] = {}
    while time.time() < deadline:
        last_turnstile = _turnstile_snapshot(page)
        slice_ev = register_events[from_index:]
        body_j, ev = _best_register_from_events(slice_ev)
        if body_j.get("code") == 1 or body_j.get("success") is True:
            return {
                "ok": True,
                "body_j": body_j,
                "event": ev,
                "turnstile": last_turnstile,
            }
        if body_j.get("code") not in (1134, None, 0) and body_j:
            code = body_j.get("code")
            if code is not None and code != 1134:
                return {
                    "ok": False,
                    "body_j": body_j,
                    "event": ev,
                    "turnstile": last_turnstile,
                }
        if last_turnstile.get("has_response") and slice_ev:
            body_j2, ev2 = _best_register_from_events(slice_ev)
            if body_j2.get("code") == 1:
                return {
                    "ok": True,
                    "body_j": body_j2,
                    "event": ev2,
                    "turnstile": last_turnstile,
                }
        page.wait_for_timeout(400)
    slice_ev = register_events[from_index:]
    body_j, ev = _best_register_from_events(slice_ev)
    return {
        "ok": body_j.get("code") == 1,
        "body_j": body_j,
        "event": ev,
        "turnstile": last_turnstile,
        "timeout": True,
    }


def _apply_register_result(result: dict[str, Any], body_j: dict[str, Any], ev: dict[str, Any] | None) -> None:
    if ev:
        result["api_base"] = ev.get("api_base") or ""
        result["register_response"] = ev.get("body")
    if body_j.get("code") == 1 or body_j.get("success") is True:
        result["ok"] = True
    elif body_j.get("code") == 1134:
        result["error"] = (
            "Turnstile chưa xong trong browser (API 1134). "
            "Thử: python c168_register.py --random --headed"
        )
    else:
        result["error"] = (
            body_j.get("msg")
            or body_j.get("message")
            or str(body_j)
            or "Đăng ký thất bại"
        )


def _capsolver_fallback(
    page,
    register_events: list[dict[str, Any]],
    *,
    cfg: dict[str, Any],
    cap_cfg: dict[str, Any],
    result: dict[str, Any],
    captcha_token: str,
    wait_ms: int,
) -> dict[str, Any]:
    """Inject token Capsolver + bấm ĐĂNG KÝ lại (khi browser mode thất bại)."""
    turnstile_token = captcha_token.strip()
    cap_kind = str(cap_cfg.get("kind") or "turnstile").lower()
    if not turnstile_token:
        solved = solve_captcha(cfg)
        result["captcha"] = solved
        if not solved.get("ok"):
            result["error"] = solved.get("error") or "Giải captcha thất bại"
            return result
        if solved.get("kind", "").startswith("geetest"):
            result["geetest_injected"] = inject_geetest_solution(page, solved)
            return result
        turnstile_token = str(solved.get("token") or "")
    if turnstile_token and not cap_kind.startswith("geetest"):
        inj = inject_turnstile_token(page, turnstile_token)
        result["turnstile_injected"] = inj.get("ok")
        result["turnstile_inject_detail"] = inj
    idx = len(register_events)
    _click_register_submit(page)
    wait = _wait_register_flow(page, register_events, from_index=idx, timeout_ms=wait_ms)
    result["turnstile_wait"] = wait.get("turnstile")
    body_j = wait.get("body_j") or {}
    if not wait.get("ok") and body_j.get("code") == 1134:
        wait = _resubmit_register_until_ok(page, register_events)
        result["turnstile_resubmit"] = wait.get("turnstile")
        body_j = wait.get("body_j") or {}
    _apply_register_result(result, body_j, wait.get("event"))
    return result


def register_account(
    *,
    username: str,
    password: str,
    phone: str,
    realname: str = "NGUYEN VAN A",
    cfg: dict[str, Any] | None = None,
    captcha_token: str = "",
    captcha_mode: str = "",
    proxy: str = "",
    use_proxy_db: bool = True,
    keep_open_ms: int = 0,
    proxy_max_tries: int = 0,
) -> dict[str, Any]:
    """Đăng ký một tài khoản — trả dict ok/username/..."""
    inp = RegisterInput(
        username=username,
        password=password,
        phone=phone,
        realname=realname,
    ).normalized()
    err = inp.validate()
    if err:
        return {"ok": False, "error": err, **inp.__dict__}

    cfg = cfg or load_config()
    base = str(cfg.get("base_url") or "https://c168b2.cc").rstrip("/")
    reg_path = str(cfg.get("register_path") or REGISTER_URL_PATH)
    pw_cfg = cfg.get("playwright") if isinstance(cfg.get("playwright"), dict) else {}
    headless = bool(pw_cfg.get("headless", True))
    timeout_ms = int(pw_cfg.get("timeout_ms") or 120_000)
    cap_cfg = dict(cfg.get("captcha") or {})
    proxy_cfg = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else {}
    max_tries = proxy_max_tries or int(proxy_cfg.get("max_tries") or 20)
    try:
        proxy_try_list = _proxy_candidates(
            explicit=proxy, use_db=use_proxy_db, cfg=cfg, max_tries=max_tries
        )
    except Exception as e:
        return {"ok": False, "error": str(e), **inp.__dict__}
    if use_proxy_db and not proxy.strip() and not proxy_try_list:
        return {
            "ok": False,
            "error": "DB LC79 không có proxy",
            **inp.__dict__,
        }

    char_delay = int(pw_cfg.get("human_delay_ms") or 0)
    pause_fields = int(pw_cfg.get("pause_between_fields_ms") or 0)
    pause_submit = int(pw_cfg.get("pause_before_submit_ms") or 0)

    result: dict[str, Any] = {
        "ok": False,
        "username": inp.username,
        "password": inp.password,
        "phone": inp.phone,
        "realname": inp.realname,
        "api_base": "",
        "register_response": None,
        "filled_fields": {},
        "captcha": {},
        "proxy": "",
    }

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        result["error"] = "Cần: pip install playwright && playwright install chromium"
        return result

    cap_mode = (
        captcha_mode.strip().lower()
        or str(cap_cfg.get("mode") or "browser").strip().lower()
    )
    if captcha_token.strip():
        cap_mode = "capsolver"
    wait_ms = int(cap_cfg.get("wait_after_submit_ms") or 90_000)

    attempts_log: list[dict[str, Any]] = []
    total = len(proxy_try_list)

    for attempt_i, proxy_str in enumerate(proxy_try_list):
        register_events: list[dict[str, Any]] = []

        def on_response(resp):
            if REGISTER_API_SUFFIX not in resp.url:
                return
            try:
                body = resp.text()[:4000]
            except Exception:
                body = ""
            register_events.append(
                {
                    "url": resp.url,
                    "status": resp.status,
                    "body": body,
                    "api_base": resp.url.split(REGISTER_API_SUFFIX)[0],
                }
            )

        attempt_result: dict[str, Any] = {
            "ok": False,
            "username": inp.username,
            "password": inp.password,
            "phone": inp.phone,
            "realname": inp.realname,
            "api_base": "",
            "register_response": None,
            "filled_fields": {},
            "captcha": {},
            "proxy": proxy_log_label(proxy_str) if proxy_str else "",
            "captcha_mode": cap_mode,
            "attempt": attempt_i + 1,
        }

        print(
            f"\n=== Proxy {attempt_i + 1}/{total}: {attempt_result['proxy']} ===",
            file=sys.stderr,
        )

        last_goto_err = ""
        page = None
        browser = None
        hold_open = keep_open_ms if (attempt_i == total - 1) else 0

        with sync_playwright() as p:
            launch_kw: dict[str, Any] = {"headless": headless}
            if headless:
                launch_kw["args"] = ["--disable-blink-features=AutomationControlled"]
            try:
                browser = p.chromium.launch(channel="chrome", **launch_kw)
            except Exception:
                browser = p.chromium.launch(**launch_kw)

            ctx_kw: dict[str, Any] = {
                "viewport": {"width": 390, "height": 844},
                "is_mobile": True,
                "has_touch": True,
                "user_agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
                ),
                "locale": "vi-VN",
            }
            if proxy_str:
                try:
                    ctx_kw["proxy"] = playwright_proxy_dict(proxy_str)
                except Exception as e:
                    attempt_result["error"] = f"Proxy lỗi: {e}"
                    attempts_log.append(
                        {
                            "proxy": attempt_result["proxy"],
                            "ok": False,
                            "error": attempt_result["error"],
                        }
                    )
                    continue

            try:
                context = browser.new_context(**ctx_kw)
            except Exception as e:
                attempt_result["error"] = f"Context lỗi: {e}"
                attempts_log.append(
                    {
                        "proxy": attempt_result["proxy"],
                        "ok": False,
                        "error": attempt_result["error"],
                    }
                )
                _close_browser(browser, None, keep_open_ms=0, headless=headless)
                continue

            context.add_init_script(
                f'localStorage.setItem("deviceId", "{uuid.uuid4()}");'
                "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
            )
            page = context.new_page()
            page.on("response", on_response)
            try:
                page.goto(
                    base + reg_path,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
            except Exception as e:
                last_goto_err = str(e)
                attempt_result["error"] = f"Không mở trang: {last_goto_err[:120]}"
                attempts_log.append(
                    {
                        "proxy": attempt_result["proxy"],
                        "ok": False,
                        "error": attempt_result["error"],
                    }
                )
                _close_browser(browser, page, keep_open_ms=0, headless=headless)
                continue

            page.wait_for_timeout(int(pw_cfg.get("page_ready_ms") or 2000))
            _ensure_register_tab(page)
            bot = _detect_bot_block(page)
            if bot:
                attempt_result["error"] = bot
                attempts_log.append(
                    {
                        "proxy": attempt_result["proxy"],
                        "ok": False,
                        "error": "chặn bot",
                    }
                )
                _close_browser(browser, page, keep_open_ms=0, headless=headless)
                if attempt_i < total - 1:
                    continue
                result.update(attempt_result)
                result["proxy_attempts"] = attempts_log
                return result

            attempt_result["filled_fields"] = _fill_by_placeholders(
                page,
                inp,
                char_delay_ms=char_delay,
                pause_between_ms=pause_fields,
            )
            if not all(attempt_result["filled_fields"].values()):
                missing = [
                    k for k, v in attempt_result["filled_fields"].items() if not v
                ]
                attempt_result["error"] = f"Không điền được các ô: {', '.join(missing)}"
                _close_browser(browser, page, keep_open_ms=hold_open, headless=headless)
                result.update(attempt_result)
                result["proxy_attempts"] = attempts_log
                return result

            _accept_terms(page)
            _pause(page, pause_submit)

            if cap_mode == "capsolver":
                attempt_result = _capsolver_fallback(
                    page,
                    register_events,
                    cfg=cfg,
                    cap_cfg=cap_cfg,
                    result=attempt_result,
                    captcha_token=captcha_token,
                    wait_ms=wait_ms,
                )
            else:
                idx = len(register_events)
                _click_register_submit(page)
                wait = _wait_register_flow(
                    page, register_events, from_index=idx, timeout_ms=wait_ms
                )
                attempt_result["turnstile_wait"] = wait.get("turnstile")
                body_j = wait.get("body_j") or {}
                ts = wait.get("turnstile") or {}
                if (
                    not wait.get("ok")
                    and body_j.get("code") == 1134
                    and ts.get("has_response")
                ):
                    attempt_result["turnstile_resubmit"] = True
                    idx2 = len(register_events)
                    _click_register_submit(page)
                    wait2 = _wait_register_flow(
                        page, register_events, from_index=idx2, timeout_ms=25_000
                    )
                    attempt_result["turnstile_wait_after_resubmit"] = wait2.get(
                        "turnstile"
                    )
                    if wait2.get("ok") or (wait2.get("body_j") or {}).get("code") == 1:
                        wait = wait2
                        body_j = wait2.get("body_j") or {}

                bot = _detect_bot_block(page)
                if bot:
                    attempt_result["error"] = bot
                elif wait.get("event"):
                    _apply_register_result(attempt_result, body_j, wait["event"])
                elif register_events[idx:]:
                    _, ev = _best_register_from_events(register_events[idx:])
                    _apply_register_result(attempt_result, body_j, ev)
                else:
                    attempt_result["error"] = (
                        page.evaluate(
                            """() => [...document.querySelectorAll('[class*="toast"],[class*="error"]')]
                              .map(e => (e.innerText||'').trim()).filter(Boolean).join(' | ')"""
                        )
                        or "Không thấy API member/register"
                    )

                if (
                    not attempt_result.get("ok")
                    and not bot
                    and cap_cfg.get("provider") == "capsolver"
                ):
                    sk = str(body_j.get("msg") or "").strip()
                    if body_j.get("code") == 1134 and re.match(r"^0x4", sk):
                        cap_cfg["sitekey"] = sk
                    attempt_result["browser_mode_failed"] = True
                    attempt_result = _capsolver_fallback(
                        page,
                        register_events,
                        cfg={**cfg, "captcha": cap_cfg},
                        cap_cfg=cap_cfg,
                        result=attempt_result,
                        captcha_token="",
                        wait_ms=wait_ms,
                    )

            hold_on_success = keep_open_ms if attempt_result.get("ok") else hold_open
            _close_browser(
                browser, page, keep_open_ms=hold_on_success, headless=headless
            )

        attempts_log.append(
            {
                "proxy": attempt_result["proxy"],
                "ok": attempt_result.get("ok"),
                "error": (attempt_result.get("error") or "")[:120],
            }
        )
        result.update(attempt_result)

        if attempt_result.get("ok"):
            print("Đăng ký OK với proxy này.", file=sys.stderr)
            break
        if _is_bot_block_error(str(attempt_result.get("error"))) and attempt_i < total - 1:
            print("Bị chặn bot — đổi proxy tiếp…", file=sys.stderr)
            continue
        if proxy.strip():
            break
        if not use_proxy_db:
            break

    result["proxy_attempts"] = attempts_log
    if not result.get("ok") and len(attempts_log) > 1:
        result["error"] = (
            f"Đã thử {len(attempts_log)} proxy từ DB, không đăng ký được. "
            f"Lần cuối: {attempts_log[-1].get('error', '')}"
        )

    if not result.get("api_base") and cfg.get("api_base_url"):
        result["api_base"] = str(cfg["api_base_url"]).rstrip("/")
    return result


def _hall_api_path(url: str) -> str:
    p = urlparse(url).path or url
    i = p.lower().find("/hall/api/")
    return p[i:] if i >= 0 else p


def _analyze_api_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        from c168_api_analyze import analyze_api_events

        return analyze_api_events(events)
    except ImportError:
        return {"api_event_count": len(events)}


def _print_analysis_summary(analysis: dict[str, Any]) -> None:
    try:
        from c168_api_analyze import print_analysis_summary

        print_analysis_summary(analysis)
    except ImportError:
        print(f"API events: {analysis.get('api_event_count', '?')}", file=sys.stderr)


def _watch_cdp_until_chrome_closed(
    *,
    cdp_url: str,
    api_events: list[dict[str, Any]],
    analysis_file: Path,
    max_minutes: int = 90,
) -> dict[str, Any]:
    """Chờ user đóng Chrome, rồi phân tích toàn bộ API đã ghi."""
    print(
        "\nĐang ghi log API — đóng cửa sổ Chrome khi bạn xong liên kết bank…",
        file=sys.stderr,
    )
    deadline = time.time() + max(5, max_minutes) * 60
    while time.time() < deadline:
        if not _cdp_is_alive(cdp_url):
            print("Chrome đã đóng — phân tích log bank…", file=sys.stderr)
            break
        time.sleep(2)
    else:
        print("Hết thời gian chờ — phân tích log hiện có…", file=sys.stderr)

    analysis = _analyze_api_events(api_events)
    analysis_file.write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _print_analysis_summary(analysis)
    print(f"\nPhân tích bank: {analysis_file}", file=sys.stderr)
    return {
        "analysis_file": str(analysis_file),
        "api_event_count": len(api_events),
        "analysis": analysis,
    }


def _log_append(path: Path, row: dict[str, Any]) -> None:
    row = {**row, "ts": datetime.now(timezone.utc).isoformat()}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _find_chrome_exe() -> str:
    candidates = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            "Google",
            "Chrome",
            "Application",
            "chrome.exe",
        ),
        os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            "Google",
            "Chrome",
            "Application",
            "chrome.exe",
        ),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return ""


def _cdp_is_alive(cdp_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{cdp_url.rstrip('/')}/json/version", timeout=2) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def _start_chrome_debug(
    *,
    base: str,
    reg_path: str,
    port: int = CDP_DEBUG_PORT,
    proxy: str = "",
) -> tuple[bool, str]:
    """Tự mở Chrome debug nếu port chưa lắng nghe. proxy → --proxy-server (SOCKS5 qua relay)."""
    chrome = _find_chrome_exe()
    if not chrome:
        return False, "Không tìm thấy chrome.exe"
    profile = _c168_chrome_profile_dir()
    url = base.rstrip("/") + reg_path
    cmd = [
        chrome,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    proxy_srv = chrome_proxy_server(proxy) if proxy.strip() else ""
    if proxy_srv:
        cmd.append(f"--proxy-server={proxy_srv}")
    cmd.append(url)
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        return False, str(e)
    cdp_url = f"http://127.0.0.1:{port}"
    for _ in range(40):
        if _cdp_is_alive(cdp_url):
            return True, cdp_url
        time.sleep(0.5)
    return False, f"Port {port} chưa sẵn sàng sau 20s"


def _launch_stealth_browser(p, *, headless: bool):
    """Chromium/Chrome ít dấu automation hơn launch mặc định."""
    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    for channel in ("chrome", "msedge"):
        try:
            return p.chromium.launch(
                channel=channel,
                headless=headless,
                args=args,
                ignore_default_args=["--enable-automation"],
            )
        except Exception:
            continue
    return p.chromium.launch(
        headless=headless,
        args=args,
        ignore_default_args=["--enable-automation"],
    )


def manual_watch_session(
    *,
    cfg: dict[str, Any] | None = None,
    proxy: str = "",
    use_proxy_db: bool = True,
    wait_minutes: int = 45,
    log_path: Path | None = None,
    connect_cdp: str = "",
    grace_seconds: int = 60,
    clear_session: bool = False,
    fill_data: RegisterInput | None = None,
    auto_start_chrome: bool = False,
    auto_submit: bool = False,
    headless_browser: bool = False,
    proxy_max_tries: int = 5,
    keep_browser_on_success_ms: int = 0,
    keep_browser_open: bool = False,
    provision_after_register: bool = False,
    bank_manual: bool = False,
    fund_password: str = "",
    bank_account: str = "",
    bank_name: str = "",
    skip_bank_bind: bool = False,
) -> dict[str, Any]:
    """
    CDP/manual: ghi API register.
    fill_data + auto_submit: điền form + bấm ĐĂNG KÝ + chờ API (tự làm hết).
    headless_browser: Chromium ẩn (không mở cửa sổ Chrome debug).
    auto_start_chrome: tự mở Chrome debug nếu port 9222 chưa chạy.
    """
    cfg = cfg or load_config()
    base = str(cfg.get("base_url") or "https://c168b2.cc").rstrip("/")
    reg_path = str(cfg.get("register_path") or REGISTER_URL_PATH)
    log_file = log_path or (_DIR / "c168_manual_log.jsonl")
    bank_analysis_file = _DIR / "c168_bank_api_analysis.json"
    if log_file.is_file():
        log_file.write_text("", encoding="utf-8")

    proxy_try = _proxy_candidates(
        explicit=proxy,
        use_db=use_proxy_db,
        cfg=cfg,
        max_tries=max(1, proxy_max_tries),
    )
    if use_proxy_db and not proxy.strip() and not proxy_try:
        return {"ok": False, "error": "DB không có proxy"}

    api_events: list[dict[str, Any]] = []
    register_hits: list[dict[str, Any]] = []
    api_hits: dict[str, list[dict[str, Any]]] = {
        suffix: [] for suffix in PROVISION_API_SUFFIXES
    }

    def on_request(req):
        if "hall/api" not in req.url:
            return
        row = {
            "kind": "request",
            "method": req.method,
            "url": req.url,
            "x_data_mode": req.headers.get("x-data-mode"),
            "post_preview": (req.post_data or "")[:2000],
        }
        api_events.append(row)
        _log_append(log_file, row)

    def on_response(resp):
        if "hall/api" not in resp.url:
            return
        try:
            body = resp.text()[:8000]
        except BaseException:
            return
        row = {
            "kind": "response",
            "status": resp.status,
            "url": resp.url,
            "path": _hall_api_path(resp.url),
            "method": resp.request.method,
            "body": body,
        }
        api_events.append(row)
        _log_append(log_file, row)
        for suffix in api_hits:
            if suffix in resp.url:
                api_hits[suffix].append(row)
                print(
                    f"\n>>> {suffix.split('/')[-1]} status={resp.status}",
                    file=sys.stderr,
                )
                break
        if REGISTER_API_SUFFIX in resp.url:
            register_hits.append(row)
            raw = (body or "").strip()
            if not raw:
                print(
                    f"\n>>> member/register status={resp.status} (body rỗng)",
                    file=sys.stderr,
                )
            else:
                try:
                    j = json.loads(raw)
                    code = j.get("code")
                    print(
                        f"\n>>> member/register code={code} msg={str(j.get('msg',''))[:80]}",
                        file=sys.stderr,
                    )
                except Exception:
                    print(
                        f"\n>>> member/register status={resp.status} "
                        f"preview={raw[:120]!r}",
                        file=sys.stderr,
                    )

    result: dict[str, Any] = {
        "ok": False,
        "mode": "manual",
        "log_file": str(log_file),
        "proxy": "",
        "page_url": base + reg_path,
        "api_event_count": 0,
        "register_calls": [],
    }

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        result["error"] = "pip install playwright && playwright install chromium"
        return result

    opened = False
    last_err = ""
    cdp_url = (connect_cdp or "").strip() or CDP_DEFAULT_URL
    click_only = fill_data is not None
    full_auto = bool(click_only and auto_submit)

    if click_only and clear_session and not headless_browser:
        print(
            "Đăng ký mới — xóa profile C168 riêng (không tắt Chrome cá nhân)…",
            file=sys.stderr,
        )
        _wipe_c168_chrome_profile(kill_chrome=True)

    use_cdp = bool(connect_cdp) and not headless_browser
    chrome_launched_by_script = False
    cdp_proxy = (proxy_try[0] if proxy_try else "").strip()
    if full_auto and not cdp_proxy:
        return {
            "ok": False,
            "error": "Đăng ký C168 bắt buộc qua proxy — dùng --proxy hoặc bỏ --no-proxy (lấy DB).",
        }
    if use_cdp and (click_only or auto_start_chrome or connect_cdp):
        connect_cdp = cdp_url
        must_launch = (
            auto_start_chrome
            or (click_only and not headless_browser)
            or (cdp_proxy and _cdp_is_alive(cdp_url))
        )
        if must_launch and (not _cdp_is_alive(cdp_url) or cdp_proxy):
            if cdp_proxy and _cdp_is_alive(cdp_url):
                print(
                    f"Đổi proxy — tắt Chrome C168 cũ, mở lại ({proxy_log_label(cdp_proxy)})…",
                    file=sys.stderr,
                )
                _wipe_c168_chrome_profile(kill_chrome=True)
            lbl = proxy_log_label(cdp_proxy) if cdp_proxy else "không proxy"
            print(
                f"Mở Chrome C168 riêng (port {CDP_DEBUG_PORT}, profile {C168_CHROME_PROFILE_NAME}) — {lbl}…",
                file=sys.stderr,
            )
            ok, msg = _start_chrome_debug(
                base=base, reg_path=reg_path, proxy=cdp_proxy
            )
            if not ok:
                return {"ok": False, "error": f"Không mở Chrome debug: {msg}"}
            chrome_launched_by_script = True
            print(f"Chrome debug OK: {msg}", file=sys.stderr)
        elif not _cdp_is_alive(cdp_url):
            return {
                "ok": False,
                "error": (
                    f"Chrome debug chưa chạy ({cdp_url}). "
                    "Chạy: start_chrome_debug.bat hoặc --click-only"
                ),
            }

    with sync_playwright() as p:
        browser = None
        context = None
        page = None

        if use_cdp and connect_cdp:
            if full_auto:
                result["mode"] = "auto_cdp"
            elif click_only:
                result["mode"] = "click_only_cdp"
            else:
                result["mode"] = "manual_cdp"
            if not click_only:
                print(
                    f"Kết nối Chrome thật qua CDP: {cdp_url}\n",
                    file=sys.stderr,
                )
            try:
                browser = p.chromium.connect_over_cdp(cdp_url)
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.pages[0] if context.pages else context.new_page()
                _bind_manual_cdp_browser(browser, on_request, on_response)
                if clear_session or click_only or not _page_needs_register(
                    page, reg_path
                ):
                    print(
                        "Mở trang đăng ký (đã xóa session / profile sạch).",
                        file=sys.stderr,
                    )
                    _clear_c168_browser_session(
                        browser, context, page, base=base, reg_path=reg_path
                    )
                    if click_only:
                        _apply_mobile_viewport(page)
                        try:
                            page.reload(
                                wait_until="domcontentloaded", timeout=120_000
                            )
                        except Exception:
                            pass
                elif REGISTER_URL_PATH not in (page.url or ""):
                    page.goto(
                        base + reg_path,
                        wait_until="domcontentloaded",
                        timeout=120_000,
                    )
                opened = True
                if cdp_proxy:
                    result["proxy"] = (
                        f"Chrome CDP + {proxy_log_label(cdp_proxy)}"
                    )
                else:
                    result["proxy"] = "Chrome thật (CDP, không proxy)"
            except Exception as e:
                result["error"] = (
                    f"Không kết nối CDP {cdp_url}: {e}. "
                    "Chạy start_chrome_debug.bat rồi thử lại."
                )
                return result
        else:
            if headless_browser and full_auto:
                result["mode"] = "auto_headless"
            for proxy_str in proxy_try:
                result["proxy"] = proxy_log_label(proxy_str) if proxy_str else ""
                mode_lbl = "headless" if headless_browser else "có cửa sổ"
                print(
                    f"Mở Chromium ({mode_lbl}) — proxy: {result['proxy'] or 'IP máy'}",
                    file=sys.stderr,
                )
                browser = _launch_stealth_browser(p, headless=headless_browser)
                ctx_kw: dict[str, Any] = {
                "viewport": {"width": 390, "height": 844},
                "is_mobile": True,
                "has_touch": True,
                "user_agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
                ),
                "locale": "vi-VN",
            }
                if proxy_str:
                    try:
                        ctx_kw["proxy"] = playwright_proxy_dict(proxy_str)
                    except Exception as e:
                        last_err = str(e)
                        browser.close()
                        continue
                try:
                    context = browser.new_context(**ctx_kw)
                    context.add_init_script(
                        f'localStorage.setItem("deviceId", "{uuid.uuid4()}");'
                        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
                    )
                    page = context.new_page()
                    _bind_manual_page_listeners(page, on_request, on_response)
                    page.goto(
                        base + reg_path,
                        wait_until="domcontentloaded",
                        timeout=120_000,
                    )
                    opened = True
                    break
                except Exception as e:
                    last_err = str(e)
                    try:
                        browser.close()
                    except Exception:
                        pass
                    continue

        if not opened:
            result["error"] = f"Không mở được trang: {last_err}"
            return result

        pw_cfg = cfg.get("playwright") if isinstance(cfg.get("playwright"), dict) else {}
        kill_cdp = False
        if click_only and fill_data:
            _apply_mobile_viewport(page)
            try:
                page.wait_for_selector(
                    'input[data-input-name="account"], input[data-input-name="userpass"]',
                    timeout=30_000,
                )
            except Exception:
                page.wait_for_timeout(1000)
            _ensure_register_tab(page)
            page.wait_for_timeout(400)
            try:
                filled = _fill_by_placeholders(
                    page,
                    fill_data,
                    char_delay_ms=int(pw_cfg.get("human_delay_ms") or 40),
                    pause_between_ms=int(pw_cfg.get("pause_between_fields_ms") or 150),
                )
            except Exception as e:
                result["error"] = f"Điền form lỗi: {e}"
                _log_append(
                    log_file,
                    {"kind": "info", "message": "session end", "result": result},
                )
                _close_browser(
                    browser,
                    page,
                    keep_open_ms=0,
                    headless=headless_browser,
                    kill_chrome=kill_cdp,
                )
                return result
            _accept_terms(page)
            result.update(
                {
                    "username": fill_data.username,
                    "password": fill_data.password,
                    "phone": fill_data.phone,
                    "realname": fill_data.realname,
                    "filled_fields": filled,
                }
            )
            if not all(filled.values()):
                missing = [k for k, v in filled.items() if not v]
                result["error"] = f"Không điền được: {', '.join(missing)}"
                return result

            cap_cfg = dict(cfg.get("captcha") or {})
            wait_ms = int(cap_cfg.get("wait_after_submit_ms") or 90_000)
            post_click_wait_ms = int(cap_cfg.get("pause_after_click_ms") or 6000)
            pause_submit = int(pw_cfg.get("pause_before_submit_ms") or 2000)

            if full_auto:
                browser_lbl = (
                    "Chromium headless (không mở cửa sổ)"
                    if headless_browser
                    else "Chrome thật (CDP)"
                )
                print(
                    "\n"
                    "══════════════════════════════════════════════════════\n"
                    f"  TỰ ĐỘNG — {browser_lbl}\n"
                    f"  TK: {fill_data.username}  MK: {fill_data.password}\n"
                    f"  SĐT: {fill_data.phone}  Tên: {fill_data.realname}\n"
                    "══════════════════════════════════════════════════════\n",
                    file=sys.stderr,
                )
                _log_append(
                    log_file,
                    {
                        "kind": "info",
                        "message": "auto_cdp started",
                        "username": fill_data.username,
                    },
                )
                _dismiss_blocking_popups(page)
                result["turnstile_before_submit"] = _turnstile_snapshot(page)
                _pause(page, pause_submit)
                idx = len(register_hits)
                print("Đang bấm ĐĂNG KÝ…", file=sys.stderr)
                try:
                    _click_register_submit(page)
                except Exception as e:
                    result["error"] = f"Không bấm được ĐĂNG KÝ: {e}"
                    _log_append(
                        log_file,
                        {"kind": "info", "message": "session end", "result": result},
                    )
                    _close_browser(
                        browser,
                        page,
                        keep_open_ms=0,
                        headless=headless_browser,
                        kill_chrome=kill_cdp,
                    )
                    return result

                wait = _wait_after_register_click(
                    page,
                    register_hits,
                    from_index=idx,
                    post_click_wait_ms=post_click_wait_ms,
                    flow_wait_ms=wait_ms,
                )
                result["post_click_wait_ms"] = post_click_wait_ms
                result["turnstile_wait"] = wait.get("turnstile")
                body_j = wait.get("body_j") or {}
                if not wait.get("ok") and body_j.get("code") == 1134:
                    wait = _resubmit_register_until_ok(page, register_hits)
                    result["turnstile_resubmit"] = wait.get("turnstile")
                    body_j = wait.get("body_j") or {}

                page.wait_for_timeout(500)
                ui_ok, via = _infer_register_success(
                    page,
                    register_hits,
                    reg_path=reg_path,
                    body_j=body_j,
                    wait_ok=bool(wait.get("ok")),
                )
                bot = _detect_bot_block(page)
                if bot:
                    result["bot_popup"] = True
                    result["error"] = bot
                elif ui_ok:
                    result["ok"] = True
                    result["success_via"] = via
                    result.pop("error", None)
                elif body_j.get("code") == 1134:
                    result["error"] = (
                        "API 1134 — Turnstile chưa hợp lệ. Thử chạy lại hoặc đợi lâu hơn."
                    )
                else:
                    result["error"] = (
                        body_j.get("msg")
                        or body_j.get("message")
                        or "Đăng ký thất bại"
                    )
                if wait.get("event"):
                    result["register_response"] = wait["event"].get("body")
                result["last_register_code"] = body_j.get("code")
                result["last_register_msg"] = body_j.get("msg")
                _finalize_manual_result(register_hits, api_events, result)
                if result.get("ok"):
                    via_s = result.get("success_via") or "ok"
                    print(
                        f"\n>>> Đăng ký thành công ({via_s}).",
                        file=sys.stderr,
                    )
                    if provision_after_register:
                        _ensure_logged_in_after_register(page, base)
                        page.wait_for_timeout(3000)
                        fp = (fund_password or "").strip() or random_fund_password()
                        ba = (bank_account or "").strip() or random_bank_account()
                        prov = run_post_register_provision(
                            page,
                            fund_password=fp,
                            bank_account=ba,
                            bank_name=bank_name,
                            realname=fill_data.realname if fill_data else "",
                            base_url=base,
                            api_hits=api_hits,
                            bank_manual=bank_manual,
                            skip_bank_bind=skip_bank_bind,
                        )
                        result["provision"] = prov
                        result["fund_password"] = fp
                        if not bank_manual:
                            result["bank_account"] = ba
                        if prov.get("phase") == "bank_manual":
                            result["bank_manual"] = True
                            print(
                                "\n>>> Sẵn sàng liên kết bank (thao tác tay).",
                                file=sys.stderr,
                            )
                        elif not prov.get("ok"):
                            result["ok"] = False
                            result["error"] = prov.get("error") or "Provision thất bại"
                        else:
                            msg = (
                                "MK rút OK (chưa liên kết bank)."
                                if prov.get("phase") == "fund_password_only"
                                else "Provision OK (MK rút + bank)."
                            )
                            print(f"\n>>> {msg}", file=sys.stderr)
                        if bank_manual and prov.get("phase") == "bank_manual":
                            keep_browser_open = True
                else:
                    print(f"\n>>> Thất bại: {result.get('error')}", file=sys.stderr)
                _log_append(
                    log_file, {"kind": "info", "message": "session end", "result": result}
                )
                if bank_manual and result.get("bank_manual"):
                    cdp_alive_url = connect_cdp or CDP_DEFAULT_URL
                    try:
                        bank_cap = _watch_cdp_until_chrome_closed(
                            cdp_url=cdp_alive_url,
                            api_events=api_events,
                            analysis_file=bank_analysis_file,
                        )
                        result["bank_capture"] = bank_cap
                        bind_rows = (
                            bank_cap.get("analysis", {})
                            .get("likely_flow", {})
                            .get("bank")
                            or []
                        )
                        if bind_rows:
                            result["bank_bind_seen"] = True
                    except Exception as e:
                        result["bank_capture_error"] = str(e)
                    try:
                        browser.close()
                    except Exception:
                        pass
                    return result

                if keep_browser_open:
                    keep_ms = -1
                else:
                    keep_ms = keep_browser_on_success_ms if result.get("ok") else 0
                    if keep_ms >= 0:
                        print("Đóng Chrome C168 sau khi chạy xong…", file=sys.stderr)
                _close_browser(
                    browser,
                    page,
                    keep_open_ms=keep_ms,
                    headless=headless_browser,
                    kill_chrome=keep_ms >= 0,
                )
                return result

            print(
                "\n"
                "══════════════════════════════════════════════════════\n"
                "  Chrome đã mở + form ĐÃ ĐIỀN.\n"
                f"  TK: {fill_data.username}  MK: {fill_data.password}\n"
                f"  SĐT: {fill_data.phone}  Tên: {fill_data.realname}\n"
                "  BẠN CHỈ CẦN bấm ĐĂNG KÝ (thêm --wait-click nếu dùng --click-only)\n"
                f"  Log: {log_file}\n"
                "══════════════════════════════════════════════════════\n",
                file=sys.stderr,
            )
        else:
            print(
                "\n"
                "══════════════════════════════════════════════════════\n"
                "  Browser đã mở — script KHÔNG điền form / KHÔNG bấm ĐĂNG KÝ.\n"
                "  Bạn tự thao tác trên Chrome.\n"
                "  QUAN TRỌNG: đợi Turnstile xong (tick xanh) RỒI mới bấm ĐĂNG KÝ.\n"
                f"  Log API: {log_file}\n"
                "  Bấm ENTER chỉ khi đã thấy ĐĂNG KÝ THÀNH CÔNG trên web.\n"
                f"  Sau ENTER script vẫn ghi log thêm {grace_seconds}s (phòng bạn bấm sớm).\n"
                "══════════════════════════════════════════════════════\n",
                file=sys.stderr,
            )
        _log_append(
            log_file,
            {
                "kind": "info",
                "message": "click_only started" if click_only else "manual session started",
                "url": base + reg_path,
                "username": fill_data.username if fill_data else None,
            },
        )

        try:
            input(
                "\nBấm ENTER khi đã thấy đăng ký THÀNH CÔNG trên Chrome "
                "(không bấm ngay khi chỉ thấy lỗi captcha)… "
            )
        except EOFError:
            print(f"(Không có stdin — chờ {wait_minutes} phút…)", file=sys.stderr)
            page.wait_for_timeout(wait_minutes * 60_000)

        if grace_seconds > 0:
            print(
                f"\nĐang ghi log thêm {grace_seconds}s — nếu vừa đăng ký xong, đợi thông báo thành công…",
                file=sys.stderr,
            )
            for left in range(grace_seconds, 0, -1):
                _finalize_manual_result(register_hits, api_events, result)
                if result.get("ok"):
                    print("\n>>> Đã bắt register code=1 — thành công.", file=sys.stderr)
                    break
                if left % 10 == 0 or left <= 5:
                    print(f"  … còn {left}s", file=sys.stderr)
                try:
                    page.wait_for_timeout(1000)
                except Exception:
                    time.sleep(1)

        _finalize_manual_result(register_hits, api_events, result)
        bot = _detect_bot_block(page)
        if bot:
            result["bot_popup"] = True
            result.setdefault("error", bot)
        if result.get("ok"):
            print(
                f"\nOK — đăng ký thành công (code=1). Log: {log_file}",
                file=sys.stderr,
            )
            result.pop("error", None)
        _log_append(log_file, {"kind": "info", "message": "session end", "result": result})
        _close_browser(browser, page, keep_open_ms=0, headless=False)

    if not result.get("ok") and not result.get("error"):
        codes = []
        for h in register_hits:
            try:
                codes.append(json.loads(h.get("body") or "{}").get("code"))
            except Exception:
                pass
        codes_s = ", ".join(str(c) for c in codes if c is not None) or "?"
        result["error"] = (
            f"Không thấy register code=1 ({len(register_hits)} lần API, codes: {codes_s}). "
            f"Bấm ENTER sau khi web báo thành công. Xem {log_file}"
        )
    return result


def save_account(path: Path, record: dict[str, Any]) -> None:
    rows: list = []
    if path.is_file():
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            rows = []
    if not isinstance(rows, list):
        rows = []
    rows.append(record)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
