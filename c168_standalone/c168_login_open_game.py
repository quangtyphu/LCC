# -*- coding: utf-8 -*-
"""
C168: user + pass → mở Chrome → đăng nhập → mở Game B (SEXY / platform 1012).

  python c168_login_open_game.py -u TAI_KHOAN -p MAT_KHAU --proxy host:port:user:pass

Mặc định: Chrome riêng acc (CMS/game_data/c168_browsers) — mở lần đầu từ CMS hoặc script tái dùng.
  --legacy-capture profile tạm %TEMP%\\c168-gameb-capture (cũ, port 9340)
  --keep-session   tái dùng Chrome đang mở / cookie profile CMS
  --skip-login     chỉ mở game (bắt buộc profile đã login)
  --login-only     chỉ đăng nhập C168, không mở Game B
  --headless-play  login C168 → Game B tab nền → đóng Chrome → cược Python (WS+HTTP)

Geetest: kéo tay trong Chrome hoặc --auto-captcha + c168_config.json.
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
import time
from typing import Any

from c168_capture_game_b import (
    CAPTURE_PORT,
    CDP_URL,
    _cdp_alive,
    _kill_capture_chrome,
    _start_chrome,
    _wipe_profile,
    configure_chrome_session,
    current_cdp_url,
    is_cms_chrome_session,
)
from c168_chrome_session import ensure_chrome_running, resolve_chrome_session
from c168_captcha_solver import inject_geetest_solution, solve_captcha
from c168_config_util import load_config
from c168_dang_nhap import _click_login_submit, _ensure_login_tab, _fill_login_form
from c168_open_game import (
    _JS_OPEN_VENDOR,
    _JS_SESSION_SNAPSHOT,
    _lobby_url,
    fetch_vendor_game_url,
    settle_after_login,
    wait_hall_session,
)
from c168_vendor_enter_table import (
    DEFAULT_TABLE_ID,
    DEFAULT_TABLE_NAME,
    enter_table_on_context,
    enter_vendor_table,
    find_vendor_page,
)
from c168_proxy import parse_proxy, proxy_log_label, resolve_register_proxy, verify_proxy_chain
from c168_register import _apply_desktop_viewport, _dismiss_blocking_popups, _pause

SITE = "https://c1686.net"
LOGIN_URL = SITE + "/home/login"
DEFAULT_PLATFORM = 1012
DEFAULT_CATEGORY = 4


def _iter_page_frames(page) -> list:
    out = [page]
    try:
        out.extend(page.frames)
    except Exception:
        pass
    return out


def _login_form_visible(page) -> bool:
    """C168: ô pass thường là data-input-name=userpass (không phải type=password)."""
    for fr in _iter_page_frames(page):
        try:
            has_pw = (
                fr.locator('input[data-input-name="userpass"]:visible').count() > 0
                or fr.locator("input[type='password']:visible").count() > 0
                or fr.locator('input[placeholder*="mật khẩu" i]:visible').count() > 0
            )
            has_acc = (
                fr.locator('input[data-input-name="account"]:visible').count() > 0
                or fr.locator('input[placeholder*="tài khoản" i]:visible').count() > 0
            )
            if has_pw:
                return True
            if has_acc and fr.locator("input:visible").count() >= 2:
                return True
        except Exception:
            continue
    return False


def _wait_login_form(page, *, timeout_ms: int = 25_000) -> bool:
    deadline = time.time() + timeout_ms / 1000.0
    selectors = (
        'input[data-input-name="userpass"]',
        'input[data-input-name="account"]',
        "input[type='password']",
    )
    while time.time() < deadline:
        if _login_form_visible(page):
            return True
        for fr in _iter_page_frames(page):
            for sel in selectors:
                try:
                    fr.locator(sel).first.wait_for(state="visible", timeout=800)
                    if _login_form_visible(page):
                        return True
                except Exception:
                    continue
        page.wait_for_timeout(400)
    return _login_form_visible(page)


def _open_login_from_header(page) -> None:
    for sel in ("[class*='un-login']", "[class*='_un-login']"):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=5000)
                page.wait_for_timeout(1200)
                return
        except Exception:
            continue


def _open_login_page(page) -> None:
    """Một lần goto tối đa — Chrome đã mở sẵn /home/login thì không reload thêm."""
    on_login_url = "/home/login" in (page.url or "").lower()

    if on_login_url and _wait_login_form(page, timeout_ms=6000):
        print("Đã ở trang login — bỏ qua reload.", file=sys.stderr)
        _dismiss_blocking_popups(page)
        return

    if not on_login_url:
        print(f"Mở form đăng nhập: {LOGIN_URL}", file=sys.stderr)
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=120_000)
    else:
        print("Trang login đang load — chờ form (không reload)…", file=sys.stderr)

    page.wait_for_timeout(2000)
    _dismiss_blocking_popups(page)
    _ensure_login_tab(page)

    if _wait_login_form(page, timeout_ms=18_000):
        return

    _open_login_from_header(page)
    _ensure_login_tab(page)
    if _wait_login_form(page, timeout_ms=10_000):
        return

    print("Form login chưa hiện — mở lại /home/login (lần cuối)…", file=sys.stderr)
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(2500)
    _dismiss_blocking_popups(page)
    _ensure_login_tab(page)
    _wait_login_form(page, timeout_ms=20_000)


def _vendor_tab_url_hint(enter_out: dict[str, Any] | None) -> str:
    """URL bàn từ health check Playwright (singleBacTable trong iframe)."""
    if not isinstance(enter_out, dict):
        return ""
    for att in reversed(enter_out.get("attempts") or []):
        if not isinstance(att, dict):
            continue
        health = att.get("health")
        if isinstance(health, dict):
            u = str(health.get("url") or "")
            if "singlebac" in u.lower():
                return u
    return ""


def _has_api_session(page) -> bool:
    try:
        snap = page.evaluate(_JS_SESSION_SNAPSHOT)
        return bool(isinstance(snap, dict) and snap.get("ready"))
    except Exception:
        return False


def _pick_hall_page(context) -> Any:
    for pg in context.pages:
        if "c1686.net" in (pg.url or ""):
            return pg
    return context.pages[0] if context.pages else context.new_page()


def _pick_vendor_page(context) -> Any | None:
    for pg in context.pages:
        ul = (pg.url or "").lower()
        if any(k in ul for k in ("bpcdf.", "tgmeq", "mhuxu", "bikimex", "vesnamex")):
            return pg
    return None


def _ensure_hall_page(page) -> None:
    if "c1686.net" not in (page.url or ""):
        page.goto(SITE + "/home", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(800)


def _get_stored_username(page) -> str:
    try:
        return (
            page.evaluate(
                """() => {
                  try {
                    const raw = localStorage.getItem("web__lobby__persisted__token");
                    if (!raw) return "";
                    const t = JSON.parse(decodeURIComponent(raw));
                    const i = t.tokenInfos || {};
                    return String(
                      i.username || i.account || i.loginName || i.name || ""
                    ).trim().toLowerCase();
                  } catch (e) { return ""; }
                }"""
            )
            or ""
        ).strip()
    except Exception:
        return ""


def _try_auto_geetest(page, cfg: dict[str, Any]) -> bool:
    cap = cfg.get("captcha") if isinstance(cfg.get("captcha"), dict) else {}
    if str(cap.get("mode") or "").lower() in ("off", "manual", ""):
        return False
    api_key = str(cap.get("api_key") or "").strip()
    if not api_key or api_key.startswith("CAP-XXXX"):
        return False
    cfg = dict(cfg)
    cfg["base_url"] = SITE
    cap = dict(cap)
    cap.setdefault("kind", "geetest_v4")
    cap["pageurl"] = cap.get("pageurl") or LOGIN_URL
    cfg["captcha"] = cap
    print("Thử giải Geetest (Capsolver)…", file=sys.stderr)
    solved = solve_captcha(cfg)
    if not solved.get("ok"):
        print(f"  Capsolver: {solved.get('error')}", file=sys.stderr)
        return False
    ok = inject_geetest_solution(page, solved)
    print(f"  inject geetest: {ok}", file=sys.stderr)
    return bool(ok)


def _login_on_page(
    page,
    *,
    username: str,
    password: str,
    cfg: dict[str, Any],
    auto_captcha: bool,
    login_timeout_sec: int,
) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "step": "login"}
    _open_login_page(page)
    if not _login_form_visible(page):
        print(
            "  Chưa thấy form login rõ — vẫn thử điền (C168: userpass/account)…",
            file=sys.stderr,
        )

    filled = _fill_login_form(page, username, password)
    out["filled_fields"] = filled
    if not all(filled.values()):
        out["error"] = f"Không điền được form: {filled}"
        return out

    _dismiss_blocking_popups(page)
    pw_cfg = cfg.get("playwright") if isinstance(cfg.get("playwright"), dict) else {}
    _pause(page, int(pw_cfg.get("pause_before_submit_ms") or 1500))
    print("Bấm ĐĂNG NHẬP…", file=sys.stderr)
    _click_login_submit(page)

    geetest_tried = False
    geetest_try_at = time.time() + 6
    deadline = time.time() + login_timeout_sec
    captcha_hint_at = time.time() + 4

    while time.time() < deadline:
        try:
            snap = page.evaluate(_JS_SESSION_SNAPSHOT)
        except Exception:
            snap = {}
        if isinstance(snap, dict) and snap.get("ready"):
            out["ok"] = True
            out["logged_in"] = True
            out["session"] = snap
            out["username"] = _get_stored_username(page) or snap.get("username") or ""
            return out

        if auto_captcha and not geetest_tried and time.time() >= geetest_try_at:
            geetest_tried = True
            if _try_auto_geetest(page, cfg):
                page.wait_for_timeout(800)
                try:
                    _click_login_submit(page)
                except Exception:
                    pass

        if time.time() > captcha_hint_at and not out.get("captcha_hint"):
            out["captcha_hint"] = True
            print(
                "\n→ Nếu có Geetest: kéo captcha trong Chrome, script chờ tối đa "
                f"{login_timeout_sec}s…\n",
                file=sys.stderr,
            )
            captcha_hint_at = time.time() + 9999

        page.wait_for_timeout(500)

    out["error"] = "login_timeout"
    snap = page.evaluate(_JS_SESSION_SNAPSHOT)
    out["session"] = snap if isinstance(snap, dict) else {}
    return out


def _open_game_on_page(
    page,
    *,
    platform_id: int,
    category_id: int,
    game_id: int,
) -> dict[str, Any]:
    snap = wait_hall_session(page, timeout_sec=30)
    if not snap.get("ready"):
        return {
            "ok": False,
            "step": "open_game",
            "error": "session_not_ready",
            "session": snap,
        }

    payload = {
        "platformId": platform_id,
        "categoryId": category_id,
        "gameId": game_id,
        "maxRetries": 5,
    }
    out = page.evaluate(_JS_OPEN_VENDOR, payload)
    if isinstance(out, dict) and out.get("ok"):
        page.wait_for_timeout(4000)
        return {
            "ok": True,
            "step": "open_game",
            "game_url": out.get("game_url"),
            "platform_id": platform_id,
            "game_id": game_id,
            "via": "current_page",
        }

    lobby = _lobby_url(category_id, platform_id)
    print(f"Mở game qua lobby (1 lần): {lobby}", file=sys.stderr)
    try:
        page.goto(lobby, wait_until="domcontentloaded", timeout=120_000)
    except Exception as e:
        print(f"  goto lobby: {e}", file=sys.stderr)
    page.wait_for_timeout(2500)

    out = page.evaluate(_JS_OPEN_VENDOR, payload)
    if not isinstance(out, dict) or not out.get("ok"):
        return {
            "ok": False,
            "step": "open_game",
            "error": out.get("error") if isinstance(out, dict) else "evaluate_failed",
            "detail": out,
        }
    page.wait_for_timeout(4000)
    return {
        "ok": True,
        "step": "open_game",
        "game_url": out.get("game_url"),
        "platform_id": platform_id,
        "game_id": game_id,
    }


def login_and_open_game(
    *,
    username: str = "",
    password: str = "",
    proxy: str = "",
    skip_proxy_check: bool = False,
    platform_id: int = DEFAULT_PLATFORM,
    category_id: int = DEFAULT_CATEGORY,
    game_id: int | None = None,
    skip_login: bool = False,
    keep_session: bool = False,
    auto_captcha: bool = False,
    login_timeout_sec: int = 120,
    keep_chrome: bool = True,
    listen_ws_sec: int = 0,
    enter_table: bool = True,
    table_name: str = DEFAULT_TABLE_NAME,
    table_id: int = DEFAULT_TABLE_ID,
    enter_table_timeout_sec: int = 90,
    auto_bet: bool = False,
    auto_bet_sec: int = 0,
    stake_min: int = 10,
    stake_max: int = 20,
    stake_unit: str = "k",
    stake_step: int = 10,
    bet_limit_id: int = 851101,
    max_bet_rounds: int = 0,
    bet_without_chrome: bool = False,
    headless_play: bool = False,
    login_only: bool = False,
    cfg: dict[str, Any] | None = None,
    legacy_capture: bool = False,
) -> dict[str, Any]:
    if game_id is None:
        game_id = platform_id * 10_000

    cfg = cfg or load_config()
    result: dict[str, Any] = {
        "ok": False,
        "username": username,
        "platform_id": platform_id,
        "game_id": game_id,
        "proxy": proxy_log_label(proxy) if proxy else "",
    }

    cms_sess = None
    if legacy_capture:
        configure_chrome_session(CDP_URL, "", cms=False)
    else:
        cms_sess = resolve_chrome_session(username=username)
        if not cms_sess:
            result["error"] = f"Không tìm thấy acc C168 trong DB: {username}"
            return result
        if not cms_sess.proxy:
            result["error"] = "Acc chưa có proxy — nhập proxy trong CMS C168 trước"
            return result
        configure_chrome_session(
            cms_sess.cdp_url, cms_sess.profile_dir, cms=cms_sess.cms
        )
        if cms_sess.proxy and not str(proxy or "").strip():
            proxy = cms_sess.proxy
        keep_session = True
        result["cdp_url"] = cms_sess.cdp_url

    if headless_play:
        auto_bet = True
        keep_chrome = False
    if login_only:
        enter_table = False
        auto_bet = False
        need_ws = False
    need_ws = auto_bet or listen_ws_sec > 0 or headless_play
    primed_sniffer = None
    skip_login_effective = skip_login

    cdp_base = current_cdp_url()

    if legacy_capture:
        if keep_session:
            if _cdp_alive(cdp_base):
                print(f"Tái dùng Chrome đang chạy ({cdp_base})", file=sys.stderr)
            else:
                print(
                    "Giữ profile c168-gameb-capture — mở Chrome (không xóa cookie/login)…",
                    file=sys.stderr,
                )
                _kill_capture_chrome()
                time.sleep(0.5)
        else:
            print(
                "Chrome mới (legacy) — xóa profile c168-gameb-capture…",
                file=sys.stderr,
            )
            _wipe_profile()
    elif _cdp_alive(cdp_base):
        print(
            f"Tái dùng Chrome C168 ({cdp_base}) — {cms_sess.username}",
            file=sys.stderr,
        )
    else:
        print(
            f"Mở Chrome C168 — {cms_sess.username}…",
            file=sys.stderr,
        )

    if proxy.strip():
        print(f"Proxy: {proxy_log_label(proxy)}", file=sys.stderr)
        if not skip_proxy_check:
            print("Kiểm tra proxy ra mạng (c1686.net) trước khi mở Chrome…", file=sys.stderr)
            ok_px, px_msg = verify_proxy_chain(proxy, timeout=22.0)
            if not ok_px:
                result["error"] = f"proxy_no_network: {px_msg}"
                print(
                    f"\n✗ Proxy không ra internet — Chrome sẽ “mất mạng” nếu vẫn mở:\n"
                    f"  {px_msg}\n"
                    f"  → Đổi proxy khác, kiểm tra host:port:user:pass, hoặc chạy không --proxy.\n"
                    f"  → Bỏ qua check: thêm --skip-proxy-check (không khuyến nghị).\n",
                    file=sys.stderr,
                )
                return result
            print(f"  Proxy OK: {px_msg}", file=sys.stderr)

    if not _cdp_alive(cdp_base):
        if cms_sess:
            ok, msg = ensure_chrome_running(cms_sess, LOGIN_URL, proxy=proxy)
        else:
            print(f"Mở Chrome legacy port {CAPTURE_PORT} → {LOGIN_URL}", file=sys.stderr)
            ok, msg = _start_chrome(LOGIN_URL, proxy=proxy)
        if not ok:
            result["error"] = msg
            return result
        print(f"Chrome: {msg}", file=sys.stderr)
        time.sleep(4.0)
        if cms_sess:
            try:
                from c168_accounts_db import get_account, update_account

                acc_row = get_account(cms_sess.username) or {}
                if not str(acc_row.get("chrome_browser_dir") or "").strip():
                    update_account(
                        cms_sess.username,
                        {
                            "chrome_browser_dir": cms_sess.profile_dir,
                            "chrome_cdp_port": cms_sess.cdp_port,
                        },
                    )
            except Exception:
                pass
    cdp_base = current_cdp_url()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        result["error"] = "pip install playwright && playwright install chromium"
        return result

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(cdp_base)
        except Exception as e:
            result["error"] = f"CDP: {e}"
            return result

        context = browser.contexts[0] if browser.contexts else browser.new_context()
        hall_page = _pick_hall_page(context)
        page = hall_page
        vendor_page = None
        _apply_desktop_viewport(page)
        already_on_vendor = False

        if need_ws:
            try:
                from c168_vendor_ws_sniff import prime_ws_capture

                print(
                    "Bật CDP nghe WS sớm (trước login/mở game) — hook kịp h54uk sảnh…",
                    file=sys.stderr,
                )
                prime_ws_capture(cdp_base)
            except RuntimeError as e:
                print(f"  ⚠ CDP sniffer: {e}", file=sys.stderr)

        if keep_session or skip_login:
            try:
                _ensure_hall_page(page)
            except Exception:
                pass

        if keep_session and not skip_login_effective and _has_api_session(page):
            skip_login_effective = True
            stored = _get_stored_username(page)
            print(
                f"Profile đã đăng nhập"
                + (f" ({stored})" if stored else "")
                + " — bỏ qua captcha/login.",
                file=sys.stderr,
            )

        if not skip_login_effective:
            if not username or not password:
                result["error"] = "Thiếu username/password"
                return result

            login_out = _login_on_page(
                page,
                username=username,
                password=password,
                cfg=cfg,
                auto_captcha=auto_captcha,
                login_timeout_sec=login_timeout_sec,
            )
            result["login"] = login_out
            if not login_out.get("ok"):
                result["error"] = login_out.get("error") or "login_failed"
                if keep_chrome:
                    print("Chrome vẫn mở — sửa captcha/login tay trên tab login.", file=sys.stderr)
                return result

            print("Chờ session hall (JWT) sau đăng nhập…", file=sys.stderr)
            settle = settle_after_login(page, site=SITE)
            result["post_login_session"] = settle
            if not settle.get("ready"):
                result["error"] = "session_not_ready_after_login"
                print(
                    f"Session chưa đủ để mở game: {settle!r} — đợi thêm hoặc login tay.",
                    file=sys.stderr,
                )
                return result

        if login_only:
            result["ok"] = True
            print(
                "Login-only: session C168 đã lưu trong profile — không mở Game B.",
                file=sys.stderr,
            )
            if keep_chrome:
                print("Giữ Chrome (port 9340).", file=sys.stderr)
            else:
                browser.close()
            return result

        elif not _has_api_session(page):
            from c168_vendor_bet import is_vendor_playable_url

            vpage = _pick_vendor_page(context)
            if skip_login and vpage and is_vendor_playable_url(vpage.url or ""):
                already_on_vendor = True
                page = vpage
                result["ok"] = True
                result["open_game"] = {
                    "ok": True,
                    "skipped": "vendor_tab",
                    "url": vpage.url,
                }
                print(
                    f"Tái dùng tab vendor đang mở — bỏ gameApi/login.\n  {vpage.url[:120]}",
                    file=sys.stderr,
                )
            else:
                result["error"] = "chua_dang_nhap"
                print(
                    "Chưa login trong profile — chạy không --skip-login (kéo captcha 1 lần) "
                    "hoặc login tay trên Chrome rồi chạy lại --keep-session.",
                    file=sys.stderr,
                )
                return result

        if already_on_vendor:
            game_out = result.get("open_game") or {"ok": True}
            vendor_page = page
        elif headless_play:
            game_out = fetch_vendor_game_url(
                hall_page,
                platform_id=platform_id,
                category_id=category_id,
                game_id=game_id,
            )
            result["open_game"] = game_out
            if game_out.get("ok"):
                gurl = str(game_out.get("game_url") or "")
                vendor_page = context.new_page()
                _apply_desktop_viewport(vendor_page)
                print(
                    "Game B: mở tab nền (API gameApi/login) — bạn chỉ cần tab C168, "
                    "không cần chơi trên web vendor.",
                    file=sys.stderr,
                )
                vendor_page.goto(gurl, wait_until="domcontentloaded", timeout=120_000)
                try:
                    hall_page.bring_to_front()
                except Exception:
                    pass
                vendor_page.wait_for_timeout(3000)
                page = vendor_page
        else:
            game_out = _open_game_on_page(
                page,
                platform_id=platform_id,
                category_id=category_id,
                game_id=game_id,
            )
            result["open_game"] = game_out
        if game_out.get("ok"):
            if not already_on_vendor:
                result["ok"] = True
                result["game_url"] = game_out.get("game_url")
                print(
                    f"\nOK — Game B (platform {platform_id}):\n"
                    f"  {str(result.get('game_url') or '')[:140]}…\n",
                    file=sys.stderr,
                )
            if need_ws:
                try:
                    from c168_vendor_ws_sniff import refresh_sniffer_targets

                    refresh_sniffer_targets()
                except Exception:
                    pass
            if not enter_table:
                print(
                    "Dừng ở sảnh vendor — bạn tự click vào phòng (vd. C06). "
                    "Khi hết thời gian WS/auto-bet, xem capture_logs/vendor_ws_*.analysis.txt",
                    file=sys.stderr,
                )
            if enter_table:
                page.wait_for_timeout(4000)
                vendor_page = find_vendor_page(context, timeout_sec=75)
                if vendor_page:
                    enter_out = enter_vendor_table(
                        vendor_page,
                        table_name=table_name,
                        table_id=table_id,
                        timeout_sec=float(enter_table_timeout_sec),
                    )
                else:
                    enter_out = enter_table_on_context(
                        context,
                        table_name=table_name,
                        table_id=table_id,
                        timeout_sec=float(enter_table_timeout_sec),
                    )
                result["enter_table"] = enter_out
                if enter_out.get("ok"):
                    try:
                        from c168_vendor_keepalive import inject_anti_idle_all
                        from c168_vendor_virtual_table import fake_enter_table_via_cdp

                        n = inject_anti_idle_all(cdp_base)
                        if n:
                            print(
                                f"  Anti-idle vendor: {n} tab (giả visible — đỡ mất WS khi đổi tab).",
                                file=sys.stderr,
                            )
                        v = fake_enter_table_via_cdp(table_id, cdp_base=cdp_base)
                        if v.get("ok"):
                            print(
                                f"  Giả vào bàn WS (lobbyTableClick tableID={table_id}) — "
                                "không cần tab game luôn hiện.",
                                file=sys.stderr,
                            )
                        else:
                            print(
                                f"  ⚠ Giả vào bàn WS: {v.get('error', v)} "
                                "(vẫn dùng click C06 nếu đã vào bàn).",
                                file=sys.stderr,
                            )
                    except Exception:
                        pass
                if enter_out.get("ok") and need_ws:
                    print(
                        "  Giữ bàn 5s — chờ WS GameInfo bàn C06 (không reload)…",
                        file=sys.stderr,
                    )
                    page.wait_for_timeout(5000)
                if not enter_out.get("ok"):
                    print(
                        f"  ⚠ Chưa vào bàn {table_name} tự động: {enter_out.get('error')}",
                        file=sys.stderr,
                    )
                    print(
                        f"  → Trong Chrome: thoát bàn lỗi (nếu có) → click {table_name} trên sảnh. "
                        "Script vẫn nghe WS + auto bet.",
                        file=sys.stderr,
                    )
        else:
            result["error"] = game_out.get("error") or "open_game_failed"

        if result.get("ok") and need_ws:
            try:
                from c168_vendor_ws_sniff import take_active_sniffer

                primed_sniffer = take_active_sniffer()
            except Exception:
                primed_sniffer = None

        if headless_play and result.get("ok"):
            from c168_vendor_session_cache import capture_vendor_session

            sn = primed_sniffer
            h54 = str(sn.h54uk_urls[-1]) if sn and sn.h54uk_urls else ""
            tab_hint = _vendor_tab_url_hint(result.get("enter_table"))
            try:
                from c168_vendor_virtual_table import (
                    fake_enter_table_via_cdp,
                    user_id_from_h54uk_url,
                )

                uid = user_id_from_h54uk_url(h54) if h54 else ""
                if uid:
                    v = fake_enter_table_via_cdp(
                        table_id, user_id=uid, cdp_base=cdp_base
                    )
                    if v.get("ok"):
                        print(
                            f"  Giữ bàn WS (lobbyTableClick) trước khi lưu session.",
                            file=sys.stderr,
                        )
                    page.wait_for_timeout(2000)
            except Exception:
                pass
            print("Lưu session cache (Chrome vẫn mở)…", file=sys.stderr)
            cap = capture_vendor_session(
                cdp_base=cdp_base,
                h54uk_url=h54,
                proxy=proxy,
                tab_hint=tab_hint,
            )
            result["session_cache"] = {
                "ok": cap.get("ok"),
                "user_id": cap.get("user_id"),
                "error": cap.get("error"),
            }
            if cap.get("ok"):
                print(
                    f"  Session OK — user …{str(cap.get('user_id') or '')[-10:]} | "
                    f"h54uk token đã lưu.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"  ✗ Session cache thất bại: {cap.get('error', cap)} — "
                    "không chạy headless được.",
                    file=sys.stderr,
                )
            result["_vendor_session"] = cap

        if headless_play and result.get("ok") and primed_sniffer:
            from c168_vendor_session_cache import apply_fresh_h54uk, save_vendor_session

            h54f = (
                str(primed_sniffer.h54uk_urls[-1])
                if primed_sniffer.h54uk_urls
                else ""
            )
            cap0 = result.get("_vendor_session") or {}
            if cap0.get("ok") and h54f:
                cap0 = apply_fresh_h54uk(cap0, h54f)
                save_vendor_session(cap0, username=username)
                result["_vendor_session"] = cap0
                from c168_vendor_session_cache import h54uk_jwt_expires_in

                ttl = h54uk_jwt_expires_in(h54f)
                print(
                    f"  Token h54uk (còn ~{max(0, int(ttl))}s) — dùng cho session cache.",
                    file=sys.stderr,
                )

        if headless_play and result.get("ok"):
            keep_chrome = True
            try:
                hall_page.bring_to_front()
            except Exception:
                pass
            print(
                "Headless-play: Chrome nền (tab Game B ẩn, bạn thấy tab C168) — "
                "cược WS+HTTP qua sniffer. Thu nhỏ Chrome được; đừng đóng tab vendor.",
                file=sys.stderr,
            )

        if keep_chrome:
            print("Giữ Chrome mở (port 9340).", file=sys.stderr)
            if proxy.strip():
                print(
                    "  Relay proxy local vẫn chạy — đừng tắt script đột ngột.",
                    file=sys.stderr,
                )
        else:
            browser.close()

    bet_sec = auto_bet_sec or listen_ws_sec

    if result.get("ok") and need_ws:
        if auto_bet and headless_play:
            from c168_vendor_auto_bet import run_auto_bet

            sc = result.get("session_cache") or {}
            if not sc.get("ok"):
                print(
                    "\n✗ Bỏ qua auto-bet — chưa lưu session.\n",
                    file=sys.stderr,
                )
                result["auto_bet"] = {
                    "seconds": 0,
                    "exit_code": 2,
                    "headless_mode": "cdp",
                    "error": "session_cache_failed",
                }
                return result

            tab_hint = _vendor_tab_url_hint(result.get("enter_table"))
            print(
                f"\nAuto cược bàn {table_name} | {stake_min}–{stake_max} ({stake_unit}) "
                f"(Chrome nền, không cần thao tác tab Game B)…\n",
                file=sys.stderr,
            )
            rc = run_auto_bet(
                0,
                table_id=table_id,
                stake_min=stake_min,
                stake_max=stake_max,
                stake_unit=stake_unit,
                stake_step=stake_step,
                bet_limit_id=bet_limit_id,
                max_rounds=max_bet_rounds,
                cdp_base=cdp_base,
                sniffer=primed_sniffer,
                vendor_tab_hint=tab_hint,
                proxy=proxy,
                headless_only=False,
                survive_chrome_close=True,
                vendor_session=result.get("_vendor_session"),
            )
            result["auto_bet"] = {
                "seconds": 0,
                "exit_code": rc,
                "headless_mode": "background_chrome",
            }
        elif auto_bet:
            from c168_vendor_auto_bet import run_auto_bet
            from c168_vendor_ws_sniff import take_active_sniffer

            if auto_bet_sec > 0:
                dur = auto_bet_sec
            elif listen_ws_sec > 0:
                dur = listen_ws_sec
                if dur < 120:
                    print(
                        f"⚠ --listen-ws {dur}s rất ngắn (~{max(1, dur // 40)} ván). "
                        f"Bỏ --listen-ws để chơi mãi, hoặc --auto-bet-sec 3600.",
                        file=sys.stderr,
                    )
            else:
                dur = 0
            sn = primed_sniffer
            if sn is None:
                sn = take_active_sniffer()
            if dur <= 0:
                dur_msg = "mãi (tắt Chrome để dừng)"
            else:
                dur_msg = f"{dur}s"
            print(
                f"\nAuto cược {dur_msg} — bàn {table_name} | "
                f"{stake_min}–{stake_max} ({stake_unit}) | chờ phiên mới…\n",
                file=sys.stderr,
            )
            tab_hint = _vendor_tab_url_hint(result.get("enter_table"))
            rc = run_auto_bet(
                dur,
                table_id=table_id,
                stake_min=stake_min,
                stake_max=stake_max,
                stake_unit=stake_unit,
                stake_step=stake_step,
                bet_limit_id=bet_limit_id,
                max_rounds=max_bet_rounds,
                cdp_base=cdp_base,
                sniffer=sn,
                vendor_tab_hint=tab_hint,
                survive_chrome_close=bet_without_chrome,
                proxy=proxy,
            )
            result["auto_bet"] = {"seconds": dur, "exit_code": rc}
        else:
            from c168_listen_ws import ListenParams, listen_vendor_ws

            print(
                f"\nNghe WebSocket vendor {listen_ws_sec}s (ban {table_name})…\n",
                file=sys.stderr,
            )
            lr = listen_vendor_ws(
                ListenParams(
                    username=username,
                    cdp_url=cdp_base,
                    table_id=table_id,
                    table_name=table_name,
                    duration_sec=listen_ws_sec,
                    sniffer=primed_sniffer,
                )
            )
            result["ws_listen"] = {
                "seconds": listen_ws_sec,
                "exit_code": lr.exit_code,
                "log": lr.log_path,
            }

    return result


def _ask(label: str, *, secret: bool = False) -> str:
    while True:
        raw = getpass.getpass(f"{label}: ") if secret else input(f"{label}: ")
        val = raw.strip()
        if val:
            return val
        print("  → Không được để trống.", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="C168: đăng nhập + mở Game B (SEXY / 1012)",
    )
    ap.add_argument("-u", "--username", default="")
    ap.add_argument("-p", "--password", default="")
    ap.add_argument(
        "--proxy",
        default="",
        help='SOCKS5 "host:port:user:pass" hoặc "host:port" (PowerShell: đặt trong dấu "...")',
    )
    ap.add_argument(
        "--proxy-from-db",
        action="store_true",
        help="Lấy proxy ngẫu nhiên từ LC79 game_data.db (c168_config.json)",
    )
    ap.add_argument(
        "--skip-proxy-check",
        action="store_true",
        help="Không test proxy trước Chrome (Chrome có thể mở nhưng mất mạng)",
    )
    ap.add_argument("--platform", type=int, default=DEFAULT_PLATFORM)
    ap.add_argument("--category", type=int, default=DEFAULT_CATEGORY)
    ap.add_argument("--game-id", type=int, default=0, help="0 = lobby platform*10000")
    ap.add_argument("--skip-login", action="store_true")
    ap.add_argument(
        "--keep-session",
        action="store_true",
        help="Tái dùng Chrome đang mở (profile CMS hoặc legacy capture)",
    )
    ap.add_argument(
        "--legacy-capture",
        action="store_true",
        help="Dùng profile tạm c168-gameb-capture port 9340 (không dùng Quản lý Chrome)",
    )
    ap.add_argument(
        "--from-db",
        action="store_true",
        help="Lấy password/proxy từ CMS game_data/c168.db",
    )
    ap.add_argument("--auto-captcha", action="store_true", help="Capsolver Geetest (c168_config.json)")
    ap.add_argument("--login-timeout", type=int, default=120)
    ap.add_argument("--close", action="store_true", help="Đóng Chrome sau khi xong")
    ap.add_argument(
        "--listen-ws",
        type=int,
        default=0,
        metavar="SEC",
        help="Sau khi mo game: nghe WS vendor SEC giay (0=tat)",
    )
    ap.add_argument(
        "--no-enter-table",
        action="store_true",
        help="Chi dung o sanh, khong tu vao ban",
    )
    ap.add_argument(
        "--table",
        default=DEFAULT_TABLE_NAME,
        help=f"Ten ban vendor (mac dinh {DEFAULT_TABLE_NAME})",
    )
    ap.add_argument(
        "--table-id",
        type=int,
        default=DEFAULT_TABLE_ID,
        help=f"tableID WS/HTTP (mac dinh {DEFAULT_TABLE_ID})",
    )
    ap.add_argument("--enter-table-timeout", type=int, default=90)
    ap.add_argument(
        "--auto-bet",
        action="store_true",
        help="Sau khi vao ban: cuoc lien tuc den Ctrl+C (khong can --listen-ws)",
    )
    ap.add_argument(
        "--auto-bet-sec",
        type=int,
        default=0,
        help="Gioi han auto bet (giay). 0 = choi mai hoac dung --listen-ws neu co",
    )
    ap.add_argument("--stake-min", type=int, default=10)
    ap.add_argument("--stake-max", type=int, default=20)
    ap.add_argument(
        "--stake-unit",
        choices=("chip", "k"),
        default="k",
        help="k: API 10/20 (=10k/20k tren san); chip: 10-20 chip",
    )
    ap.add_argument(
        "--stake-step",
        type=int,
        default=10,
        help="Boi khi --stake-unit k (10,20 = chi 10k hoac 20k)",
    )
    ap.add_argument("--bet-limit", type=int, default=851101, help="betLimitID")
    ap.add_argument("--max-bet-rounds", type=int, default=0, help="0=khong gioi han")
    ap.add_argument(
        "--bet-without-chrome",
        action="store_true",
        help="Thu: tắt Chrome vẫn cược (HTTP + WS Python, cần session cache)",
    )
    ap.add_argument(
        "--headless-play",
        action="store_true",
        help="Login C168 → Game B tab nền (ẩn) → đóng Chrome → cược Python WS+HTTP",
    )
    ap.add_argument(
        "--login-only",
        action="store_true",
        help="Chỉ đăng nhập C168, lưu profile — không mở Game B",
    )
    args = ap.parse_args()

    if not args.skip_login:
        if not args.username:
            print("\n══ C168 — Login + Game B ══\n", file=sys.stderr)
            args.username = _ask("Tên tài khoản")
        if not args.password:
            args.password = _ask("Mật khẩu", secret=True)

    cfg = load_config()
    try:
        proxy = resolve_register_proxy(
            explicit=args.proxy.strip(),
            cfg=cfg,
            use_db=args.proxy_from_db,
        )
    except (ValueError, RuntimeError) as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    gid = args.game_id if args.game_id > 0 else None
    uname = args.username.strip()
    pwd = args.password
    if args.from_db and uname:
        try:
            from c168_accounts_db import get_account_by_username

            acc = get_account_by_username(uname)
            if acc:
                if not pwd:
                    pwd = str(acc.get("password") or "")
                if not args.proxy.strip() and str(acc.get("proxy") or "").strip():
                    proxy = str(acc["proxy"]).strip()
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"from-db: {e}"}), file=sys.stderr)
            return 1

    out = login_and_open_game(
        username=uname,
        password=pwd,
        proxy=proxy,
        skip_proxy_check=args.skip_proxy_check,
        legacy_capture=args.legacy_capture,
        platform_id=args.platform,
        category_id=args.category,
        game_id=gid,
        skip_login=args.skip_login,
        keep_session=args.keep_session,
        auto_captcha=args.auto_captcha,
        login_timeout_sec=args.login_timeout,
        keep_chrome=not args.close,
        listen_ws_sec=max(0, int(args.listen_ws or 0)),
        enter_table=not args.no_enter_table,
        table_name=args.table.strip(),
        table_id=args.table_id,
        enter_table_timeout_sec=args.enter_table_timeout,
        auto_bet=args.auto_bet,
        auto_bet_sec=max(0, int(args.auto_bet_sec or 0)),
        stake_min=args.stake_min,
        stake_max=args.stake_max,
        stake_unit=args.stake_unit,
        stake_step=args.stake_step,
        bet_limit_id=args.bet_limit,
        max_bet_rounds=max(0, int(args.max_bet_rounds or 0)),
        bet_without_chrome=args.bet_without_chrome,
        headless_play=args.headless_play,
        login_only=args.login_only,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
