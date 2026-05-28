# -*- coding: utf-8 -*-
"""
C168: mở Chrome (nếu chưa chạy) → check session → vào C06 → nghe WS (phiên / kết quả).
- Mặc định không đặt cược. Bật cược: --auto-bet.

  python c168_post_open_chrome.py -u hoangnam47
  python c168_post_open_chrome.py -u hoangnam47 --check-only
  python c168_post_open_chrome.py -u hoangnam47 --auto-bet
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from c168_capture_game_b import _cdp_alive, configure_chrome_session, current_cdp_url
from c168_chrome_session import (
    ChromeSession,
    SITE_LOGIN_URL,
    chrome_session_from_override,
    ensure_chrome_running,
    resolve_chrome_session,
)
from c168_login_open_game import (
    DEFAULT_CATEGORY,
    DEFAULT_PLATFORM,
    _ensure_hall_page,
    _has_api_session,
    _open_game_on_page,
    _pick_hall_page,
    _pick_vendor_page,
    _vendor_tab_url_hint,
)
from c168_open_game import _JS_SESSION_SNAPSHOT
from c168_vendor_enter_table import (
    DEFAULT_TABLE_ID,
    DEFAULT_TABLE_NAME,
    enter_table_on_context,
    enter_vendor_table,
    find_vendor_page,
)

DEFAULT_LISTEN_SEC = int(
    __import__("os").environ.get("C168_WS_LISTEN_SEC", str(86400 * 7))
)


def _session_snapshot(page) -> dict[str, Any]:
    try:
        snap = page.evaluate(_JS_SESSION_SNAPSHOT)
        return snap if isinstance(snap, dict) else {}
    except Exception:
        return {}


def run_post_open_chrome(
    *,
    username: str = "",
    check_only: bool = False,
    auto_bet: bool = False,
    auto_bet_sec: int = 0,
    listen_sec: int = 0,
    stake_min: int = 10,
    stake_max: int = 20,
    stake_unit: str = "k",
    stake_step: int = 10,
    bet_limit_id: int = 851101,
    max_bet_rounds: int = 0,
    cdp_wait_sec: float = 6.0,
    table_name: str = DEFAULT_TABLE_NAME,
    table_id: int = DEFAULT_TABLE_ID,
    enter_table_timeout_sec: int = 90,
    launch_chrome: bool = True,
    chrome_session: ChromeSession | None = None,
) -> dict[str, Any]:
    cms = chrome_session or resolve_chrome_session(username=username)
    if not cms:
        return {
            "ok": False,
            "error": f"Không tìm thấy acc C168: {username}",
        }

    configure_chrome_session(cms.cdp_url, cms.profile_dir, cms=True)
    cdp_base = current_cdp_url()

    def _wait_cdp(sec: float) -> bool:
        if sec <= 0:
            return _cdp_alive(cdp_base)
        deadline = time.time() + sec
        while time.time() < deadline:
            if _cdp_alive(cdp_base):
                return True
            time.sleep(0.35)
        return _cdp_alive(cdp_base)

    chrome_running = _cdp_alive(cdp_base)
    out: dict[str, Any] = {
        "ok": True,
        "username": cms.username,
        "cdp_url": cdp_base,
        "chrome_running": chrome_running,
        "session_alive": False,
        "action": "none",
    }

    if chrome_running:
        print(
            f"Chrome đang chạy ({cdp_base}) — {cms.username}: đọc session…",
            file=sys.stderr,
        )
    elif not launch_chrome:
        wait_sec = max(0.0, float(cdp_wait_sec or 0))
        if not chrome_running and wait_sec > 0:
            print(
                f"Chờ CDP {wait_sec:.0f}s sau Mở Chrome CMS ({cdp_base})…",
                file=sys.stderr,
            )
            chrome_running = _wait_cdp(wait_sec)
        if not chrome_running:
            out["ok"] = False
            out["error"] = (
                f"Chrome CDP chưa sẵn sàng ({cdp_base}) — đợi vài giây rồi bấm Mở Chrome lại"
            )
            out["action"] = "none"
            return out
    else:
        print(
            f"Chrome chưa chạy — mở {cms.username} CDP :{cms.cdp_port}…",
            file=sys.stderr,
        )
        ok_launch, launch_msg = ensure_chrome_running(cms, SITE_LOGIN_URL)
        out["chrome_launch"] = {"ok": ok_launch, "message": launch_msg}
        if not ok_launch:
            out["ok"] = False
            out["error"] = launch_msg
            out["action"] = "none"
            return out
        print(f"Chrome: {launch_msg}", file=sys.stderr)
        try:
            from c168_accounts_db import get_account, update_account

            acc_row = get_account(cms.username) or {}
            if not str(acc_row.get("chrome_browser_dir") or "").strip():
                update_account(
                    cms.username,
                    {
                        "chrome_browser_dir": cms.profile_dir,
                        "chrome_cdp_port": cms.cdp_port,
                    },
                )
        except Exception:
            pass
        cdp_base = current_cdp_url()
        out["cdp_url"] = cdp_base
        out["chrome_running"] = True
        configure_chrome_session(cdp_base, cms.profile_dir, cms=True)
        if cdp_wait_sec > 0:
            time.sleep(cdp_wait_sec)

    if not _cdp_alive(cdp_base):
        out["ok"] = False
        out["error"] = f"Chrome CDP chưa sẵn sàng ({cdp_base})"
        out["action"] = "none"
        return out

    out["chrome_running"] = True

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        out["ok"] = False
        out["error"] = "pip install playwright && playwright install chromium"
        return out

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(cdp_base)
        except Exception as e:
            out["ok"] = False
            out["error"] = f"CDP: {e}"
            return out

        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = _pick_hall_page(context)
        try:
            _ensure_hall_page(page)
            page.wait_for_timeout(1500)
        except Exception:
            pass

        snap = _session_snapshot(page)
        out["session"] = snap

        if not _has_api_session(page):
            out["message"] = (
                "Session C168 không còn (logout / hết hạn) — chỉ mở Chrome, không vào BCR"
            )
            print(out["message"], file=sys.stderr)
            return out

        out["session_alive"] = True
        if check_only:
            out["message"] = "Session C168 còn sống — sẵn sàng nghe WS (mặc định không cược)"
            return out

        print(
            f"Session OK — {cms.username}: vào BCR {table_name} (tableID={table_id})…",
            file=sys.stderr,
        )

        try:
            from c168_vendor_ws_sniff import prime_ws_capture

            prime_ws_capture(cdp_base)
        except RuntimeError as e:
            out["ok"] = False
            out["error"] = str(e)
            out["action"] = "ws_sniffer_failed"
            return out

        vendor = _pick_vendor_page(context)
        if not vendor:
            print("Chưa có tab vendor — mở Game B…", file=sys.stderr)
            game_id = DEFAULT_PLATFORM * 10_000
            game_out = _open_game_on_page(
                page,
                platform_id=DEFAULT_PLATFORM,
                category_id=DEFAULT_CATEGORY,
                game_id=game_id,
            )
            out["open_game"] = game_out
            if not game_out.get("ok"):
                out["action"] = "open_game_failed"
                out["message"] = "Session sống nhưng mở Game B thất bại"
                return out
            page.wait_for_timeout(5500)

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
        out["enter_table"] = enter_out

        if not enter_out.get("ok"):
            out["action"] = "enter_table_failed"
            out["message"] = (
                f"Session sống nhưng vào bàn {table_name} thất bại — không nghe WS"
            )
            print(out["message"], file=sys.stderr)
            return out

        try:
            from c168_vendor_keepalive import inject_anti_idle_all
            from c168_vendor_virtual_table import fake_enter_table_via_cdp

            inject_anti_idle_all(cdp_base)
            fake_enter_table_via_cdp(table_id, cdp_base=cdp_base)
        except Exception:
            pass

        try:
            from c168_vendor_session_cache import refresh_session_from_chrome

            h54 = ""
            try:
                from c168_vendor_ws_sniff import _ACTIVE_SNIFFER, refresh_sniffer_targets

                refresh_sniffer_targets()
                if _ACTIVE_SNIFFER is not None:
                    _ACTIVE_SNIFFER.reinject_ws_hooks()
                    if _ACTIVE_SNIFFER.h54uk_urls:
                        h54 = str(_ACTIVE_SNIFFER.h54uk_urls[-1])
                primed_sniffer = _ACTIVE_SNIFFER
            except Exception:
                primed_sniffer = None
            sc = refresh_session_from_chrome(
                username=cms.username,
                h54uk_url=h54,
                proxy=cms.proxy or "",
            )
            out["session_cache"] = {
                "ok": sc.get("ok"),
                "user_id": sc.get("user_id"),
                "error": sc.get("error"),
            }
            time.sleep(0.5)
        except Exception:
            primed_sniffer = None

        if vendor_page:
            try:
                from c168_vendor_enter_table import _table_health

                h = _table_health(
                    vendor_page, table_name=table_name, table_id=table_id
                )
                out["table_health_after_enter"] = h
                if not h.get("healthy"):
                    print(
                        f"  Cảnh báo: chưa xác nhận UI bàn {table_name} "
                        f"(onTable={h.get('onTable')}, url={str(h.get('url') or '')[:80]}). "
                        "WS có thể chỉ là sảnh — mở tab Sexy và vào lại bàn nếu không thấy PHIEN MOI.",
                        file=sys.stderr,
                    )
            except Exception:
                pass

        tab_hint = _vendor_tab_url_hint(enter_out)
        proxy = cms.proxy or ""

        if not auto_bet:
            dur = listen_sec if listen_sec > 0 else DEFAULT_LISTEN_SEC
            out["action"] = "ws_listen"
            out["listen_sec"] = dur
            out["message"] = f"Nghe WS BCR {table_name} — không đặt cược ({dur}s max)"
            print(
                f"\n══ Nghe WS — {table_name} tableID={table_id} (không cược) ══\n"
                f"  PHIEN MOI | KET QUA PHIEN trên bàn này\n"
                f"  CDP: {cdp_base} | Ctrl+C dừng\n",
                file=sys.stderr,
            )
            from c168_listen_ws import ListenParams, listen_vendor_ws

            lr = listen_vendor_ws(
                ListenParams(
                    username=cms.username,
                    cdp_url=cdp_base,
                    table_id=table_id,
                    table_name=table_name,
                    duration_sec=dur,
                    sniffer=primed_sniffer,
                )
            )
            out["listen_exit_code"] = lr.exit_code
            out["listen_log"] = lr.log_path
            out["listen_stats"] = {
                "recv": lr.recv_frames,
                "parsed": lr.parsed_frames,
                "round_events": lr.round_events,
            }
            return out

        if auto_bet_sec > 0:
            dur = auto_bet_sec
        elif listen_sec > 0:
            dur = listen_sec
        else:
            dur = 0
        dur_msg = f"{dur}s" if dur > 0 else "không giới hạn (Ctrl+C dừng)"
        out["action"] = "auto_bet"
        out["auto_bet_sec"] = dur
        out["table_name"] = table_name
        out["table_id"] = table_id
        out["message"] = f"Auto bet BCR {table_name} — {dur_msg}"

        print(
            f"\n══ Auto bet (c168_vendor_auto_bet) — {table_name} | "
            f"{stake_min}–{stake_max} {stake_unit} | {dur_msg} ══\n"
            f"  In: Phiên mới → đặt cược → Kết quả ván (giống c168_login_open_game --auto-bet)\n",
            file=sys.stderr,
        )

        from c168_vendor_auto_bet import run_auto_bet

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
            sniffer=primed_sniffer,
            vendor_tab_hint=tab_hint,
            proxy=proxy,
            survive_chrome_close=False,
        )
        out["auto_bet_exit_code"] = rc
        return out


def _emit_json_result(result: dict[str, Any]) -> None:
    """JSON một dòng ra stdout cho CMS (log tiếng Việt ở stderr)."""
    line = json.dumps(result, ensure_ascii=False) + "\n"
    try:
        sys.stdout.buffer.write(line.encode("utf-8"))
        sys.stdout.buffer.flush()
    except Exception:
        print(json.dumps(result, ensure_ascii=True), flush=True)


def _ask_username() -> str:
    while True:
        raw = input("Username C168: ").strip()
        if raw:
            return raw
        print("  → Không được để trống.", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="C168 sau Mở Chrome: check session + WS C06",
        epilog=(
            "Mặc định: session OK → vào C06 + nghe WS (phiên / kết quả), KHÔNG đặt cược.\n"
            "Ví dụ:\n"
            "  python c168_post_open_chrome.py -u hoangnam47\n"
            "  python c168_post_open_chrome.py -u hoangnam47 --check-only\n"
            "  python c168_post_open_chrome.py -u hoangnam47 --auto-bet  # khi còn tiền"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("-u", "--username", default="", help="Username trong c168.db")
    ap.add_argument(
        "--profile-dir",
        default="",
        help="Profile AllGame (allgame_browsers/...) — bỏ qua c168.db",
    )
    ap.add_argument("--cdp-port", type=int, default=0, help="CDP port (kèm --profile-dir)")
    ap.add_argument("--proxy", default="", help="SOCKS5 (kèm --profile-dir)")
    ap.add_argument(
        "--check-only",
        action="store_true",
        help="Chỉ kiểm tra session",
    )
    ap.add_argument(
        "--auto-bet",
        action="store_true",
        help="Bật đặt cược tự động (mặc định TẮT — chỉ nghe WS)",
    )
    ap.add_argument("--auto-bet-sec", type=int, default=0, help="Giới hạn auto bet (giây), 0=không giới hạn")
    ap.add_argument(
        "--listen-sec",
        type=int,
        default=0,
        help="Giới hạn nghe WS (giây), 0=mặc định 7 ngày",
    )
    ap.add_argument("--stake-min", type=int, default=10)
    ap.add_argument("--stake-max", type=int, default=20)
    ap.add_argument("--stake-unit", choices=("chip", "k"), default="k")
    ap.add_argument("--stake-step", type=int, default=10)
    ap.add_argument("--bet-limit", type=int, default=851101)
    ap.add_argument("--max-bet-rounds", type=int, default=0, help="0=không giới hạn số ván cược")
    ap.add_argument(
        "--no-launch",
        action="store_true",
        help="Không tự mở Chrome (CDP phải đang chạy, ví dụ vừa bấm Mở Chrome trên CMS)",
    )
    ap.add_argument("--cdp-wait", type=float, default=6.0, help="Giây chờ Chrome sau khi mở")
    ap.add_argument("--table", default=DEFAULT_TABLE_NAME)
    ap.add_argument("--table-id", type=int, default=DEFAULT_TABLE_ID)
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    username = str(args.username or "").strip()
    if not username:
        print("\n══ C168 — Kiểm tra session / WS C06 ══\n", file=sys.stderr)
        username = _ask_username()

    chrome_session: ChromeSession | None = None
    profile_dir = str(args.profile_dir or "").strip()
    if profile_dir:
        chrome_session = chrome_session_from_override(
            username=username,
            profile_dir=profile_dir,
            cdp_port=int(args.cdp_port or 0),
            proxy=str(args.proxy or "").strip(),
        )
        if not chrome_session:
            _emit_json_result({"ok": False, "error": "Thiếu username hoặc --profile-dir"})
            return 1

    launch_chrome = not args.no_launch
    if chrome_session is not None and profile_dir:
        launch_chrome = False

    result = run_post_open_chrome(
        username=username,
        chrome_session=chrome_session,
        check_only=args.check_only,
        auto_bet=bool(args.auto_bet),
        auto_bet_sec=max(0, int(args.auto_bet_sec or 0)),
        listen_sec=max(0, int(args.listen_sec or 0)),
        stake_min=args.stake_min,
        stake_max=args.stake_max,
        stake_unit=args.stake_unit,
        stake_step=args.stake_step,
        bet_limit_id=args.bet_limit,
        max_bet_rounds=max(0, int(args.max_bet_rounds or 0)),
        cdp_wait_sec=float(args.cdp_wait or 0),
        table_name=str(args.table or DEFAULT_TABLE_NAME).strip(),
        table_id=int(args.table_id or DEFAULT_TABLE_ID),
        launch_chrome=launch_chrome,
    )
    _emit_json_result(result)
    if not result.get("ok"):
        return 1
    if result.get("session_alive") and result.get("action") == "none":
        return 0
    if result.get("action") == "ws_listen":
        return int(result.get("listen_exit_code") or 0)
    if result.get("action") == "auto_bet":
        return int(result.get("auto_bet_exit_code") or 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
