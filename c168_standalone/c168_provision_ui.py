# -*- coding: utf-8 -*-
"""Sau đăng ký C168: MK rút → liên kết bank (thao tác UI trên Chrome CDP)."""
from __future__ import annotations

import random
import re
import string
import sys
import time
from typing import Any
from urllib.parse import urlparse

from c168_browser_api import set_fund_password_via_browser_api

API_MODIFY_WITHDRAW = "/hall/api/member/user/security/modifyWithdrawPass"
API_VERIFY_WITHDRAW_PASS = "/hall/api/member/user/security/verifyWithdrawPass"
API_VERIFY_WITHDRAW_V2 = "/hall/api/finance/certify/verifyWithdrawalPasswordV2"
API_BINDCARD = "/hall/api/finance/certify/bindcard"
API_WITHDRAW_INFO = "/hall/api/finance/certify/withdrawInfoV2"
API_WITHDRAW_SETTING = "/hall/api/finance/certify/withdrawSettingV3"

PROVISION_API_SUFFIXES = (
    API_MODIFY_WITHDRAW,
    API_VERIFY_WITHDRAW_PASS,
    API_VERIFY_WITHDRAW_V2,
    API_BINDCARD,
    API_WITHDRAW_INFO,
    API_WITHDRAW_SETTING,
)


def random_fund_password() -> str:
    return "".join(random.choices(string.digits, k=6))


def random_bank_account() -> str:
    n = random.randint(10, 13)
    return "".join(random.choices(string.digits, k=n))


def _pause(page, ms: int) -> None:
    if ms > 0:
        page.wait_for_timeout(ms)


def _body_text(page) -> str:
    try:
        return page.evaluate(
            "() => (document.body && document.body.innerText) || ''"
        ) or ""
    except Exception:
        return ""


def _click_by_inner_text(page, keywords: list[str]) -> bool:
    """Click phần tử visible có chứa keyword (button/a/div/span)."""
    try:
        hit = page.evaluate(
            """(keys) => {
              const low = keys.map(k => k.toLowerCase());
              const nodes = document.querySelectorAll(
                'button, a, [role=button], span, div, p, li'
              );
              for (const el of nodes) {
                const t = (el.innerText || '').trim();
                if (!t || t.length > 80) continue;
                const tl = t.toLowerCase();
                if (!low.some(k => tl.includes(k))) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 8 || r.height < 8) continue;
                const st = getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') continue;
                el.click();
                return t.slice(0, 60);
              }
              return '';
            }""",
            keywords,
        )
        if hit:
            _pause(page, 700)
            return True
    except Exception:
        pass
    return False


def _click_text(
    page,
    patterns: list[str],
    *,
    timeout_ms: int = 12_000,
    exact: bool = False,
) -> bool:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        _dismiss_popups(page)
        for pat in patterns:
            try:
                loc = page.get_by_text(pat, exact=exact)
                if loc.count():
                    for i in range(min(loc.count(), 6)):
                        el = loc.nth(i)
                        try:
                            if el.is_visible():
                                el.click(timeout=5000, force=True)
                                _pause(page, 600)
                                return True
                        except Exception:
                            continue
            except Exception:
                pass
            try:
                loc = page.locator(f"text=/{pat}/i")
                if loc.count():
                    loc.first.click(timeout=5000, force=True)
                    _pause(page, 600)
                    return True
            except Exception:
                pass
        if _click_by_inner_text(page, [pat.lower() for pat in patterns]):
            return True
        _pause(page, 400)
    return False


def _dismiss_popups(page) -> None:
    low = _body_text(page).lower()
    if any(
        x in low
        for x in (
            "mật khẩu rút",
            "mat khau rut",
            "nhập lại mật khẩu",
            "xác nhận mật khẩu",
        )
    ):
        return
    for label in (
        "Huỷ Bỏ",
        "Hủy Bỏ",
        "Đóng",
        "Bỏ qua",
        "Cancel",
        "Không",
        "Để sau",
        "Đã hiểu",
        "OK",
    ):
        try:
            btn = page.get_by_role("button", name=label)
            if btn.count() and btn.first.is_visible():
                btn.first.click(force=True, timeout=2000)
                _pause(page, 350)
        except Exception:
            continue
    try:
        page.locator(
            "button:has-text('Huỷ'), button:has-text('Hủy'), .ui-dialog__close"
        ).first.click(force=True, timeout=1500)
        _pause(page, 300)
    except Exception:
        pass


def _fill_visible_inputs(
    page,
    value: str,
    *,
    max_fields: int = 4,
    input_type: str | None = None,
) -> int:
    """Điền các ô input/password/tel còn trống (theo thứ tự trên)."""
    filled = 0
    sel = "input:visible, textarea:visible"
    if input_type:
        sel = f"input[type='{input_type}']:visible"
    count = page.locator(sel).count()
    for i in range(min(count, max_fields)):
        el = page.locator(sel).nth(i)
        try:
            cur = el.input_value(timeout=1500)
        except Exception:
            cur = ""
        if cur and cur.strip():
            continue
        try:
            el.scroll_into_view_if_needed(timeout=5000)
            el.fill(value, timeout=8000, force=True)
            filled += 1
            _pause(page, 200)
        except Exception:
            try:
                el.evaluate(
                    """(el, v) => {
                      el.focus();
                      el.value = v;
                      el.dispatchEvent(new Event('input', { bubbles: true }));
                      el.dispatchEvent(new Event('change', { bubbles: true }));
                    }""",
                    value,
                )
                filled += 1
                _pause(page, 200)
            except Exception:
                pass
    return filled


def _type_pin_keypad(page, pin: str) -> bool:
    """Bàn phím số từng ô (MK rút 6 số)."""
    ok = 0
    for ch in pin:
        if not ch.isdigit():
            continue
        clicked = page.evaluate(
            """(d) => {
              const nodes = document.querySelectorAll(
                'button, div, span, li, [role=button]'
              );
              for (const el of nodes) {
                const t = (el.innerText || '').trim();
                if (t !== d) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 10 || r.height < 10) continue;
                el.click();
                return true;
              }
              return false;
            }""",
            ch,
        )
        if clicked:
            ok += 1
            _pause(page, 150)
            continue
        for sel in (
            f"button:has-text('{ch}')",
            f"[data-key='{ch}']",
            f".keyboard button:has-text('{ch}')",
        ):
            try:
                loc = page.locator(sel)
                if loc.count():
                    loc.first.click(force=True, timeout=3000)
                    ok += 1
                    _pause(page, 120)
                    break
            except Exception:
                continue
    return ok >= len(pin)


def _normalize_pin(pin: str) -> str:
    return re.sub(r"\D", "", str(pin or ""))[:6]


def _fill_six_digit_boxes(page, pin: str) -> bool:
    pin = _normalize_pin(pin)
    if len(pin) != 6:
        return False
    try:
        ok = page.evaluate(
            """(pin) => {
              const vis = (el) => {
                const r = el.getBoundingClientRect();
                if (r.width < 6 || r.height < 6) return false;
                const s = getComputedStyle(el);
                return s.display !== 'none' && s.visibility !== 'hidden';
              };
              const fire = (el, v) => {
                el.focus();
                el.value = v;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
              };
              const inputs = [...document.querySelectorAll('input')].filter(vis);
              const boxes = inputs.filter(
                (el) => (el.maxLength === 1 || el.getAttribute('maxlength') === '1')
              );
              if (boxes.length >= 6) {
                for (let i = 0; i < 6; i++) fire(boxes[i], pin[i] || '');
                return true;
              }
              const tel = inputs.filter(
                (el) => el.type === 'tel' || el.inputMode === 'numeric'
              );
              if (tel.length >= 1) {
                fire(tel[0], pin);
                if (tel[1]) fire(tel[1], pin);
                return true;
              }
              const pw = inputs.filter((el) => el.type === 'password');
              if (pw.length >= 1) {
                for (const el of pw.slice(0, 2)) fire(el, pin);
                return true;
              }
              return false;
            }""",
            pin,
        )
        if ok:
            _pause(page, 400)
            return True
    except Exception:
        pass
    return False


def _fill_van_password_group(page, pin: str, group_index: int = 0) -> bool:
    """Vue van-password-input: 6 ô — click ô đầu rồi gõ từng số."""
    pin = _normalize_pin(pin)
    if len(pin) != 6:
        return False
    selectors = (
        ".van-password-input",
        "[class*='password-input']",
        "[class*='withdraw-password']",
        "[class*='fund-password']",
    )
    for sel in selectors:
        try:
            groups = page.locator(sel)
            if groups.count() <= group_index:
                continue
            g = groups.nth(group_index)
            g.scroll_into_view_if_needed(timeout=5000)
            items = g.locator("li, [class*='item'], input")
            if items.count() >= 6:
                items.nth(0).click(force=True, timeout=5000)
                _pause(page, 150)
                for i, ch in enumerate(pin):
                    if i > 0:
                        try:
                            items.nth(i).click(force=True, timeout=2000)
                        except Exception:
                            pass
                    page.keyboard.type(ch, delay=90)
                    _pause(page, 60)
                return True
            g.click(force=True, timeout=5000)
            _pause(page, 200)
            page.keyboard.type(pin, delay=100)
            return True
        except Exception:
            continue
    return False


def _type_pin_all_groups(page, pin: str) -> bool:
    """Điền PIN cho mọi nhóm 6 ô trên màn (đặt + xác nhận)."""
    pin = _normalize_pin(pin)
    if len(pin) != 6:
        return False
    ok_any = False
    try:
        n_groups = page.locator(".van-password-input").count()
    except Exception:
        n_groups = 0
    if n_groups >= 1:
        for gi in range(min(n_groups, 3)):
            if _fill_van_password_group(page, pin, gi):
                ok_any = True
                _pause(page, 500)
        if ok_any:
            return True
    if _fill_six_digit_boxes(page, pin):
        return True
    try:
        labels = page.get_by_text(re.compile(r"xác nhận.*mật khẩu rút", re.I))
        if labels.count():
            labels.first.click(timeout=3000)
            _pause(page, 200)
    except Exception:
        pass
    if _fill_van_password_group(page, pin, 1):
        return True
    return _type_pin_keypad(page, pin)


def _fill_fund_password(page, pwd: str) -> bool:
    if _type_pin_all_groups(page, pwd):
        return True
    if _fill_six_digit_boxes(page, pwd):
        return True
    n_pw = page.locator("input[type='password']:visible").count()
    if n_pw >= 1:
        for i in range(min(n_pw, 3)):
            try:
                page.locator("input[type='password']:visible").nth(i).fill(
                    pwd, force=True, timeout=8000
                )
                _pause(page, 150)
            except Exception:
                pass
        return True
    n_num = page.locator(
        "input[type='number']:visible, input[type='tel']:visible, "
        "input[inputmode='numeric']:visible"
    ).count()
    if n_num >= 1:
        for i in range(min(n_num, 3)):
            try:
                page.locator(
                    "input[type='number']:visible, input[type='tel']:visible, "
                    "input[inputmode='numeric']:visible"
                ).nth(i).fill(pwd, force=True, timeout=8000)
                _pause(page, 150)
            except Exception:
                pass
        return True
    if _type_pin_keypad(page, pwd):
        return True
    try:
        page.locator("input:visible").first.click(force=True, timeout=5000)
        page.keyboard.type(pwd, delay=80)
        _pause(page, 300)
        return True
    except Exception:
        pass
    return _fill_visible_inputs(page, pwd, max_fields=3) > 0


def _submit_form(page) -> bool:
    for label in (
        "Xác nhận",
        "Xác Nhận",
        "Hoàn tất",
        "Lưu",
        "Tiếp theo",
        "Gửi",
        "Liên kết",
        "Thêm thẻ",
        "Đồng ý",
        "OK",
    ):
        try:
            btn = page.get_by_role("button", name=label)
            if btn.count():
                for i in range(min(btn.count(), 4)):
                    b = btn.nth(i)
                    if b.is_visible():
                        b.click(force=True, timeout=8000)
                        _pause(page, 800)
                        return True
        except Exception:
            continue
    try:
        page.locator("button.ui-button--primary:visible").last.click(
            force=True, timeout=8000
        )
        _pause(page, 800)
        return True
    except Exception:
        return False


def _wait_post_register_login(page, *, timeout_ms: int = 35_000) -> None:
    """Sau register: đóng popup, chờ rời /register (session đăng nhập)."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        url = (page.url or "").lower()
        low = _body_text(page).lower()
        if "/register" not in url and "/login" not in url:
            break
        if "đăng ký thành công" in low or "chúc mừng" in low:
            _click_text(
                page,
                ["Vào trang chủ", "Bắt đầu", "Nạp ngay", "Trang chủ", "OK"],
                timeout_ms=2500,
            )
        _dismiss_popups(page)
        _pause(page, 700)
    _pause(page, 2000)


def _effective_site_base(page, base_url: str) -> str:
    """Dùng host thật sau redirect (vd c1686.net thay vì c168b2.cc)."""
    try:
        u = urlparse(page.url or "")
        if u.scheme and u.netloc and "c168" in u.netloc.lower():
            return f"{u.scheme}://{u.netloc}"
    except Exception:
        pass
    return base_url.rstrip("/")


def _security_fund_password_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    redirect = (
        "%7B%22name%22%3A%22withdraw%22%2C%22query%22%3A%7B%22active%22%3A20%7D%7D"
    )
    return f"{base}/home/security?active=5&redirect={redirect}"


def _open_security_fund_password_page(page, base_url: str) -> bool:
    """Màn Cài đặt MK rút (active=5) — verifyWithdrawPass + modifyWithdrawPass."""
    url = _security_fund_password_url(base_url)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    except Exception:
        return False
    _pause(page, 2500)
    _dismiss_popups(page)
    low = _body_text(page).lower()
    return any(
        x in low
        for x in (
            "mật khẩu rút",
            "mat khau rut",
            "cài đặt mật khẩu",
            "xác nhận mật khẩu",
        )
    ) or page.locator(".van-password-input, input").count() > 0


def _set_fund_password_on_security_page(
    page,
    fund_password: str,
    api_hits: dict[str, list],
    *,
    base_url: str,
) -> bool:
    """Đặt MK rút trên /home/security?active=5 — chờ verify + modify API."""
    pin = _normalize_pin(fund_password)
    if len(pin) != 6:
        return False

    idx_verify = len(api_hits.get(API_VERIFY_WITHDRAW_PASS) or [])
    idx_mod = len(api_hits.get(API_MODIFY_WITHDRAW) or [])

    site_base = _effective_site_base(page, base_url)
    if not _open_security_fund_password_page(page, site_base):
        print(
            f"[PROVISION] Không mở được {site_base}/home/security?active=5",
            file=sys.stderr,
        )
        return False

    print("[PROVISION] Màn security — nhập MK rút (6 ô × 2)…", file=sys.stderr)
    _pause(page, 800)

    # Nhóm 1: đặt MK
    if not _fill_van_password_group(page, pin, 0):
        _type_pin_all_groups(page, pin)
    _pause(page, 600)

    # Nhóm 2: xác nhận
    if not _fill_van_password_group(page, pin, 1):
        try:
            page.locator(".van-password-input").nth(1).click(force=True, timeout=5000)
            _pause(page, 200)
            page.keyboard.type(pin, delay=100)
        except Exception:
            _fill_six_digit_boxes(page, pin)
    _pause(page, 500)

    _submit_fund_password(page)
    _pause(page, 1500)

    if _wait_api(
        api_hits, API_VERIFY_WITHDRAW_PASS, from_index=idx_verify, timeout_ms=20_000
    ):
        print("[PROVISION] verifyWithdrawPass OK", file=sys.stderr)
    if _wait_api(
        api_hits, API_MODIFY_WITHDRAW, from_index=idx_mod, timeout_ms=35_000
    ):
        print("[PROVISION] modifyWithdrawPass OK", file=sys.stderr)
        return True

    # Thử submit lần 2 (một số bản tự gửi sau đủ 12 số)
    _submit_fund_password(page)
    if _wait_api(
        api_hits, API_MODIFY_WITHDRAW, from_index=idx_mod, timeout_ms=20_000
    ):
        return True

    low = _body_text(page).lower()
    if "thành công" in low or "đặt thành công" in low:
        return True
    return False


def _open_fund_password_via_profile(page) -> bool:
    _click_bottom_tab(page, 4)
    _pause(page, 1000)
    _dismiss_popups(page)
    if _click_text(
        page,
        ["Bảo mật", "Trung tâm", "Cá nhân", "Tài khoản", "Hội viên"],
        timeout_ms=6000,
    ):
        _pause(page, 1000)
    return _click_text(
        page,
        [
            "Mật khẩu rút",
            "MK rút",
            "Mật khẩu giao dịch",
            "Mật khẩu rút tiền",
        ],
        timeout_ms=8000,
    ) or _click_by_inner_text(page, ["mật khẩu rút", "mk rút"])


def _wait_logged_in_home(page, *, timeout_ms: int = 25_000) -> None:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        _dismiss_popups(page)
        url = (page.url or "").lower()
        if "/register" not in url:
            break
        low = _body_text(page).lower()
        if "đăng ký thành công" in low or "chúc mừng" in low:
            _dismiss_popups(page)
            _click_text(page, ["Vào trang chủ", "Trang chủ", "Bắt đầu", "OK"], timeout_ms=3000)
        _pause(page, 800)
    _pause(page, 2000)
    for _ in range(4):
        _dismiss_popups(page)
        _pause(page, 500)


def _withdraw_api_count(api_hits: dict[str, list]) -> int:
    return len(api_hits.get(API_WITHDRAW_INFO) or []) + len(
        api_hits.get(API_WITHDRAW_SETTING) or []
    )


def _click_bottom_tab(page, index: int) -> None:
    try:
        page.evaluate(
            """(idx) => {
              const sels = [
                '.van-tabbar-item',
                '.tabbar-item',
                '[class*="tab-bar"] [class*="item"]',
                'footer a',
                '[class*="TabBar"] button',
              ];
              for (const sel of sels) {
                const items = document.querySelectorAll(sel);
                if (items.length >= idx + 1) {
                  items[idx].click();
                  return true;
                }
              }
              return false;
            }""",
            index,
        )
        _pause(page, 1200)
    except Exception:
        pass


def _open_withdraw(page, api_hits: dict[str, list], base_url: str) -> bool:
    before = _withdraw_api_count(api_hits)
    base = base_url.rstrip("/")

    def _ready() -> bool:
        return _withdraw_api_count(api_hits) > before

    for path in (
        "/home/withdraw",
        "/withdraw",
        "/home/mine/withdraw",
        "/home/center/withdraw",
    ):
        try:
            page.goto(
                base + path,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            _pause(page, 2000)
            _dismiss_popups(page)
            if _ready():
                return True
        except Exception:
            continue

    _dismiss_popups(page)
    for tab_idx in (3, 4, 2):
        _click_bottom_tab(page, tab_idx)
        _dismiss_popups(page)
        if _ready():
            return True

    idx_info = len(api_hits.get(API_WITHDRAW_INFO) or [])
    if _click_text(
        page,
        ["Rút tiền", "Rút Tiền", "Withdraw", "提现"],
        timeout_ms=8000,
    ) and _wait_api(
        api_hits, API_WITHDRAW_INFO, from_index=idx_info, timeout_ms=12_000
    ):
        return True
    if _click_by_inner_text(page, ["rút tiền"]) and _ready():
        return True

    try:
        page.locator('[href*="withdraw"], [data-route*="withdraw"]').first.click(
            force=True, timeout=8000
        )
        _pause(page, 1500)
        if _ready():
            return True
    except Exception:
        pass

    deadline = time.time() + 12
    while time.time() < deadline:
        if _ready():
            return True
        _pause(page, 400)
    return False


def _open_bank_link(page) -> bool:
    _dismiss_popups(page)
    labels = [
        "Liên kết bank",
        "Liên kết ngân hàng",
        "Liên kết thẻ",
        "Liên kết tài khoản",
        "Thêm thẻ",
        "Thêm ngân hàng",
        "Thêm tài khoản",
        "Liên kết",
        "Ngân hàng",
        "Tài khoản nhận",
        "Quản lý thẻ",
    ]
    if _click_text(page, labels, timeout_ms=10_000):
        return True
    if _click_by_inner_text(
        page,
        ["liên kết", "ngân hàng", "thêm thẻ", "tài khoản nhận", "bind"],
    ):
        return True
    return False


def _prompt_set_fund_password(page) -> bool:
    """Bấm CTA đặt MK rút trên màn rút."""
    return _click_text(
        page,
        [
            "Đặt mật khẩu rút",
            "Thiết lập mật khẩu",
            "Tạo mật khẩu rút",
            "Mật khẩu rút tiền",
            "Chưa có mật khẩu",
            "Thiết lập ngay",
            "Đi đến thiết lập",
            "Thiết lập",
        ],
        timeout_ms=8000,
    ) or _click_by_inner_text(
        page, ["mật khẩu rút", "thiết lập", "đặt mật khẩu"]
    )


def _enter_fund_password_dialog(page, fund_password: str) -> None:
    """MK rút: 6 ô × 2 (đặt + xác nhận)."""
    try:
        page.locator(
            ".ui-dialog:visible, .van-dialog:visible, [class*='modal']:visible"
        ).first.click(force=True, timeout=3000)
    except Exception:
        pass
    _pause(page, 400)
    _type_pin_all_groups(page, fund_password)
    _pause(page, 400)


def _submit_fund_password(page) -> None:
    for _ in range(3):
        if _submit_form(page):
            return
        _pause(page, 500)
    _click_by_inner_text(page, ["xác nhận", "hoàn tất", "lưu"])


def _bank_name_candidates(bank_name: str) -> list[str]:
    """Tên hiển thị trên UI — thử tên gốc + alias phổ biến."""
    raw = (bank_name or "").strip()
    if not raw:
        return []
    up = raw.upper()
    aliases: dict[str, list[str]] = {
        "VCB": ["Vietcombank", "VCB"],
        "VIETCOMBANK": ["Vietcombank", "VCB"],
        "TCB": ["Techcombank", "TCB"],
        "TECHCOMBANK": ["Techcombank", "TCB"],
        "MB": ["MB Bank", "MB", "MBBank"],
        "MBBANK": ["MB Bank", "MB", "MBBank"],
        "MSB": ["MSB", "Maritime Bank"],
        "VPB": ["VPBank", "VPB"],
        "VPBANK": ["VPBank", "VPB"],
        "ACB": ["ACB"],
        "BIDV": ["BIDV"],
        "VIB": ["VIB"],
        "TPB": ["TPBank", "TPB"],
        "TPBANK": ["TPBank", "TPB"],
        "SHB": ["SHB"],
        "HDB": ["HDBank", "HDB"],
        "HDBANK": ["HDBank", "HDB"],
        "OCB": ["OCB"],
        "SCB": ["SCB"],
        "STB": ["Sacombank", "STB"],
        "SACOMBANK": ["Sacombank", "STB"],
        "AGRIBANK": ["Agribank"],
    }
    out: list[str] = [raw]
    for key, vals in aliases.items():
        if key in up.replace(" ", ""):
            for v in vals:
                if v not in out:
                    out.append(v)
    return out


def _pick_bank(page, bank_name: str = "") -> bool:
    """Mở dropdown ngân hàng và chọn NH (ưu tiên bank_name)."""
    _dismiss_popups(page)
    bank_hints = [
        "Chọn ngân hàng",
        "Ngân hàng",
        "Tên ngân hàng",
        "NH",
    ]
    for h in bank_hints:
        try:
            loc = page.get_by_text(h, exact=False)
            if loc.count() and loc.first.is_visible():
                loc.first.click(force=True, timeout=5000)
                _pause(page, 500)
                break
        except Exception:
            continue
    try:
        page.locator(
            ".ui-select, .van-picker, .bank-list, [class*='bank']"
        ).first.click(force=True, timeout=5000)
        _pause(page, 500)
    except Exception:
        pass
    for name in _bank_name_candidates(bank_name) + [
        "Vietcombank",
        "VCB",
        "Techcombank",
        "TCB",
        "MB Bank",
        "MB",
        "ACB",
        "BIDV",
        "VPBank",
        "Agribank",
        "MSB",
    ]:
        if _click_text(page, [name], timeout_ms=3000):
            return True
    try:
        opts = page.locator(
            ".van-picker-column__item, .ui-select-option, li:visible, "
            "[class*='bank-item']:visible"
        )
        if opts.count() >= 2:
            opts.nth(1).click(force=True, timeout=5000)
            _pause(page, 400)
            _submit_form(page)
            return True
    except Exception:
        pass
    return False


def _api_hit_seen(api_hits: dict[str, list], suffix: str, from_index: int) -> bool:
    rows = api_hits.get(suffix) or []
    return len(rows) > from_index


def _wait_api(
    api_hits: dict[str, list],
    suffix: str,
    *,
    from_index: int,
    timeout_ms: int = 45_000,
) -> bool:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if _api_hit_seen(api_hits, suffix, from_index):
            return True
        time.sleep(0.35)
    return False


def _is_cipher_ok(body: str, status: int) -> bool:
    raw = (body or "").strip()
    if status != 200 or len(raw) < 24:
        return False
    if "1134" in raw or '"code":0' in raw:
        try:
            j = __import__("json").loads(raw)
            if j.get("code") not in (1, None):
                return j.get("code") == 1
        except Exception:
            pass
    if '"code":1' in raw:
        return True
    sample = re.sub(r"\s+", "", raw[:200])
    return bool(re.fullmatch(r"[A-Za-z0-9+/=_-]+", sample))


def run_post_register_provision(
    page,
    *,
    fund_password: str,
    bank_account: str = "",
    bank_name: str = "",
    realname: str = "",
    base_url: str = "https://c168b2.cc",
    api_hits: dict[str, list] | None = None,
    timeout_ms: int = 180_000,
    bank_manual: bool = False,
    skip_bank_bind: bool = False,
) -> dict[str, Any]:
    """
    UI: Rút tiền → đặt MK rút → (tuỳ chọn) liên kết bank.
    bank_manual=True: dừng sau mở màn bank + thử xác minh MK rút; user tự nhập STK/NH.
    skip_bank_bind=True: chỉ đặt MK rút, bỏ qua liên kết bank.
    """
    api_hits = api_hits if api_hits is not None else {}
    out: dict[str, Any] = {
        "ok": False,
        "fund_password": fund_password,
        "bank_account": bank_account,
        "steps": [],
    }
    if skip_bank_bind:
        print(f"\n[PROVISION] MK rút={fund_password}", file=sys.stderr)
    else:
        print(
            f"\n[PROVISION] MK rút={fund_password} STK={bank_account}",
            file=sys.stderr,
        )
    _wait_post_register_login(page)
    _wait_logged_in_home(page)
    try:
        page.goto(
            base_url.rstrip("/") + "/",
            wait_until="domcontentloaded",
            timeout=90_000,
        )
    except Exception:
        pass
    _pause(page, 2500)
    for _ in range(5):
        _dismiss_popups(page)
        _pause(page, 400)

    idx_mod = len(api_hits.get(API_MODIFY_WITHDRAW) or [])

    api_browser = set_fund_password_via_browser_api(page, fund_password)
    out["steps"].append(
        {
            "step": "set_fund_password_api",
            "ok": bool(api_browser.get("ok")),
            "via": "browser_http_client",
            "detail": {
                k: api_browser.get(k)
                for k in ("error", "api_origin", "http_clients", "verify_ok")
                if api_browser.get(k) is not None
            },
        }
    )

    ok_security = bool(api_browser.get("ok"))
    if ok_security:
        _pause(page, 800)
        if not _api_hit_seen(api_hits, API_MODIFY_WITHDRAW, idx_mod):
            _pause(page, 1200)
        ok_security = _api_hit_seen(
            api_hits, API_MODIFY_WITHDRAW, idx_mod
        ) or bool(api_browser.get("ok"))
        out["steps"].append(
            {
                "step": "set_fund_password",
                "ok": ok_security,
                "via": "browser_api",
                "api": _api_hit_seen(api_hits, API_MODIFY_WITHDRAW, idx_mod),
            }
        )
    else:
        print("[PROVISION] API browser thất bại — thử UI security…", file=sys.stderr)
        ok_security = _set_fund_password_on_security_page(
            page, fund_password, api_hits, base_url=base_url
        )
        out["steps"].append(
            {
                "step": "set_fund_password",
                "ok": ok_security,
                "via": "security_page",
                "api": _api_hit_seen(api_hits, API_MODIFY_WITHDRAW, idx_mod),
            }
        )

    if not ok_security:
        print("[PROVISION] Security thất bại — thử màn Rút tiền…", file=sys.stderr)
        if not _open_withdraw(page, api_hits, base_url):
            out["error"] = "Không đặt được MK rút (security + withdraw)"
            out["steps"].append({"step": "open_withdraw", "ok": False})
            return out
        out["steps"].append({"step": "open_withdraw", "ok": True})
        _pause(page, 1500)
        _prompt_set_fund_password(page)
        _pause(page, 1200)
        _type_pin_all_groups(page, fund_password)
        _submit_fund_password(page)
        _pause(page, 1200)
        if not _api_hit_seen(api_hits, API_MODIFY_WITHDRAW, idx_mod):
            _open_fund_password_via_profile(page)
            _pause(page, 1500)
            _type_pin_all_groups(page, fund_password)
            _submit_fund_password(page)
        ok_api = _wait_api(
            api_hits, API_MODIFY_WITHDRAW, from_index=idx_mod, timeout_ms=45_000
        )
        out["steps"][-1] = {
            "step": "set_fund_password",
            "ok": ok_api,
            "via": "withdraw_fallback",
            "api": ok_api,
        }
        if not ok_api and not bank_manual:
            out["error"] = "Không thấy API modifyWithdrawPass"
            return out
    else:
        if not _open_withdraw(page, api_hits, base_url):
            out["steps"].append({"step": "open_withdraw", "ok": False})
        else:
            out["steps"].append({"step": "open_withdraw", "ok": True})
        _pause(page, 1500)
        _dismiss_popups(page)

    fund_step = next(
        (s for s in reversed(out["steps"]) if s.get("step") == "set_fund_password"),
        {},
    )
    fund_ok = bool(fund_step.get("ok"))
    if skip_bank_bind:
        out["ok"] = fund_ok
        out["phase"] = "fund_password_only"
        if fund_ok:
            print(
                "[PROVISION] MK rút OK — tạm bỏ qua liên kết bank.",
                file=sys.stderr,
            )
        else:
            out["error"] = out.get("error") or "Không đặt được MK rút tiền"
        return out

    if bank_manual:
        if not _open_bank_link(page):
            if not _open_withdraw(page, api_hits, base_url):
                out["error"] = "Không mở Liên kết bank"
                out["steps"].append({"step": "open_bank", "ok": False})
                return out
            if not _open_bank_link(page):
                out["error"] = "Không mở Liên kết bank"
                out["steps"].append({"step": "open_bank", "ok": False})
                return out
        out["steps"].append({"step": "open_bank", "ok": True})
        _pause(page, 1200)
        idx_verify = len(api_hits.get(API_VERIFY_WITHDRAW_V2) or [])
        print(
            "[PROVISION] Thử nhập MK rút (xác minh trước khi bind) — bạn có thể sửa tay…",
            file=sys.stderr,
        )
        _enter_fund_password_dialog(page, fund_password)
        _submit_form(page)
        verify_ok = _wait_api(
            api_hits,
            API_VERIFY_WITHDRAW_V2,
            from_index=idx_verify,
            timeout_ms=20_000,
        )
        out["steps"].append(
            {"step": "verify_fund_password", "ok": verify_ok, "api": verify_ok}
        )
        out["phase"] = "bank_manual"
        out["ok"] = True
        out["message"] = (
            "MK rút đã xử lý. Bạn tự nhập STK + chọn NH + gửi. "
            "Đóng Chrome khi xong — script phân tích log."
        )
        print(
            "\n══════════════════════════════════════════════════════\n"
            f"  MK rút (6 số): {fund_password}\n"
            "  Bước tiếp: nhập lại MK rút (nếu web hỏi) → STK → ngân hàng → Gửi\n"
            "  ĐÓNG Chrome khi xong — script ghi log & phân tích\n"
            "══════════════════════════════════════════════════════\n",
            file=sys.stderr,
        )
        return out

    if not _open_bank_link(page):
        if not _open_withdraw(page, api_hits, base_url):
            out["error"] = "Không mở Liên kết bank"
            out["steps"].append({"step": "open_bank", "ok": False})
            return out
        if not _open_bank_link(page):
            out["error"] = "Không mở Liên kết bank"
            out["steps"].append({"step": "open_bank", "ok": False})
            return out
    out["steps"].append({"step": "open_bank", "ok": True})
    _pause(page, 1200)

    idx_verify = len(api_hits.get(API_VERIFY_WITHDRAW_V2) or [])
    print("[PROVISION] Nhập MK rút xác nhận…", file=sys.stderr)
    if not _fill_fund_password(page, fund_password):
        out["error"] = "Không điền MK rút (xác nhận bank)"
        out["steps"].append({"step": "verify_fund_password", "ok": False})
        return out
    _submit_form(page)
    if not _wait_api(
        api_hits, API_VERIFY_WITHDRAW_V2, from_index=idx_verify, timeout_ms=25_000
    ):
        out["error"] = "Không thấy API verifyWithdrawalPasswordV2"
        out["steps"].append({"step": "verify_fund_password", "ok": False})
        return out
    out["steps"].append({"step": "verify_fund_password", "ok": True})
    _pause(page, 1200)

    idx_bind = len(api_hits.get(API_BINDCARD) or [])
    print("[PROVISION] STK + ngân hàng…", file=sys.stderr)
    _fill_visible_inputs(page, bank_account, max_fields=2, input_type="tel")
    _fill_visible_inputs(page, bank_account, max_fields=2)
    if realname:
        _fill_visible_inputs(page, realname.upper(), max_fields=1)
    _pick_bank(page, bank_name)
    _pause(page, 500)
    _submit_form(page)
    if not _wait_api(api_hits, API_BINDCARD, from_index=idx_bind, timeout_ms=35_000):
        _submit_form(page)
        _wait_api(api_hits, API_BINDCARD, from_index=idx_bind, timeout_ms=15_000)

    bind_rows = api_hits.get(API_BINDCARD) or []
    bind_ok = False
    if len(bind_rows) > idx_bind:
        ev = bind_rows[-1]
        bind_ok = _is_cipher_ok(str(ev.get("body") or ""), int(ev.get("status") or 0))
    out["steps"].append({"step": "bind_bank", "ok": bind_ok})
    if bind_ok:
        out["ok"] = True
        print("[PROVISION] Liên kết bank OK.", file=sys.stderr)
    else:
        out["error"] = "bindcard chưa thành công"
        print("[PROVISION] bindcard thất bại.", file=sys.stderr)
    return out
