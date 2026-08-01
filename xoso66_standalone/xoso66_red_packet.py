# -*- coding: utf-8 -*-
"""
Nhận lì xì XOSO66 — flow đầy đủ (1 phiên Playwright).

1. Login API — KHÔNG gọi getredpacketinfo trước khi mở web
2. /home/ → đợi load → click mưa (đánh dấu đã biết sự kiện)
3. Đợi popup → bấm 「Mở bao lì xì」 (gọi grabredpacket từ web)
4. Nếu đã tắt popup: bấm 「Được nhận」 (≈ getredpacketinfo) → 「Mở bao lì xì」
5. Nếu UI chưa nhận được: thử POST grabredpacket qua HTTP (cùng cookie phiên)

CLI:
  python xoso66_red_packet.py cuhoangtoan
  python xoso66_red_packet.py acc16
  python xoso66_red_packet.py all
  python xoso66_red_packet.py all --parallel 5
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BATCH_PARALLEL_DEFAULT = 5
_print_lock = threading.Lock()

DIR = Path(__file__).resolve().parent
LOG_DIR = DIR / "red_packet_logs"

PATH_INFO = "/server/user/getredpacketinfo"
PATH_GRAB = "/server/user/grabredpacket"

# Hardcode — không đọc xoso66_config.json
RED_PACKET_ENABLED = True
RED_PACKET_ON_PLAYWRIGHT_TOKEN = True
RED_PACKET_ONCE_PER_VN_DAY = True
RED_PACKET_LOAD_WAIT_SEC = 3.0
RED_PACKET_POPUP_WAIT_SEC = 12.0
RED_PACKET_AFTER_OPEN_SEC = 6.0
RED_PACKET_HTTP_FALLBACK = True


@dataclass
class RedPacketTimings:
    load_wait_sec: float = 5.0
    popup_wait_sec: float = 15.0
    after_open_sec: float = 8.0
    after_info_sec: float = 2.0


def _parse_api_body(data: Any) -> dict[str, Any]:
    if isinstance(data, dict) and "_decrypt_error" in data:
        m = re.search(r"\{.*\}", data.get("_cipher_preview", ""))
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
        return {"_raw": data}
    return data if isinstance(data, dict) else {"_raw": data}


def _parse_body_text(body: str) -> dict[str, Any] | None:
    t = (body or "").strip()
    if t.startswith('"') and t.endswith('"'):
        t = t[1:-1]
    if t.startswith("{"):
        try:
            return json.loads(t)
        except Exception:
            pass
    return None


def fetch_red_packet_info(session: dict) -> dict[str, Any]:
    """GET getredpacketinfo — mở lại danh sách sau khi tắt popup."""
    from xoso66_bank_bind import _get_encrypted

    _, raw = _get_encrypted(session, PATH_INFO, {})
    out = _parse_api_body(raw)
    data = out.get("data") or {}
    lst = data.get("list") or []
    return {
        "ok": out.get("code") == 1,
        "code": out.get("code"),
        "msg": out.get("msg"),
        "isNew": data.get("isNew"),
        "list": lst,
        "red_notify": data.get("red_notify") or [],
        "packet_id": lst[0].get("id") if lst else None,
        "raw": out,
    }


def grab_red_packet(session: dict, packet_id: int | str) -> dict[str, Any]:
    """POST grabredpacket."""
    from xoso66_session import post_encrypted

    _, raw, _ = post_encrypted(session, PATH_GRAB, {"id": int(packet_id)})
    out = _parse_api_body(raw)
    return {
        "ok": out.get("code") == 1,
        "code": out.get("code"),
        "msg": out.get("msg"),
        "data": out.get("data"),
        "packet_id": packet_id,
        "raw": out,
    }


def red_packet_cfg(_cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Cấu hình lì xì cố định trong code (không dùng config file)."""
    return {
        "enabled": RED_PACKET_ENABLED,
        "on_playwright_token": RED_PACKET_ON_PLAYWRIGHT_TOKEN,
        "once_per_vn_day": RED_PACKET_ONCE_PER_VN_DAY,
        "load_wait_sec": RED_PACKET_LOAD_WAIT_SEC,
        "popup_wait_sec": RED_PACKET_POPUP_WAIT_SEC,
        "after_open_sec": RED_PACKET_AFTER_OPEN_SEC,
        "http_fallback": RED_PACKET_HTTP_FALLBACK,
    }


def timings_from_red_packet_cfg(rp_cfg: dict[str, Any] | None = None) -> RedPacketTimings:
    c = rp_cfg or red_packet_cfg()
    return RedPacketTimings(
        load_wait_sec=float(c["load_wait_sec"]),
        popup_wait_sec=float(c["popup_wait_sec"]),
        after_open_sec=float(c["after_open_sec"]),
        after_info_sec=2.0,
    )


_CLAIM_DDL = """
CREATE TABLE IF NOT EXISTS red_packet_claim_log (
    account_id TEXT NOT NULL,
    vn_day TEXT NOT NULL,
    claimed INTEGER NOT NULL DEFAULT 0,
    amount_vnd INTEGER NOT NULL DEFAULT 0,
    msg TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, vn_day)
)
"""


def _init_claim_log_db() -> None:
    from xoso66_accounts_db import db_conn, init_db

    init_db()
    with db_conn() as conn:
        conn.execute(_CLAIM_DDL)


def red_packet_claimed_ok_today(account_id: str) -> bool:
    from xoso66_time_util import today_vn_str

    aid = str(account_id or "").strip()
    if not aid:
        return False
    _init_claim_log_db()
    day = today_vn_str()
    from xoso66_accounts_db import db_conn

    with db_conn() as conn:
        row = conn.execute(
            "SELECT claimed FROM red_packet_claim_log WHERE account_id = ? AND vn_day = ?",
            (aid, day),
        ).fetchone()
    return bool(row and int(row["claimed"] or 0) == 1)


def _save_claim_log(account_id: str, result: dict[str, Any]) -> None:
    from datetime import datetime, timezone
    from xoso66_time_util import today_vn_str

    aid = str(account_id or "").strip()
    if not aid:
        return
    _init_claim_log_db()
    from xoso66_accounts_db import db_conn

    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO red_packet_claim_log
                (account_id, vn_day, claimed, amount_vnd, msg, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, vn_day) DO UPDATE SET
                claimed = excluded.claimed,
                amount_vnd = excluded.amount_vnd,
                msg = excluded.msg,
                updated_at = excluded.updated_at
            """,
            (
                aid,
                today_vn_str(),
                1 if result.get("claimed") else 0,
                int(result.get("amount_received") or 0),
                str(result.get("msg") or result.get("error") or "")[:500],
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def _log_red_packet(user: str, msg: str) -> None:
    print(f"[RED-PACKET] {user} | {msg}", flush=True)


def try_claim_red_packet_on_home_page(
    page: Any,
    session: dict,
    account_id: str,
    *,
    persist: bool = True,
) -> dict[str, Any]:
    """
    Nhận lì xì trên page đã mở /home/ (piggyback refresh_user_token_playwright).
    Không mở browser mới — in log [RED-PACKET] từng bước / API.
    """
    from xoso66_accounts_db import username_for_log
    from xoso66_deposit import apply_response_tokens
    from xoso66_session import get_user_balance, persist_session

    aid = str(account_id or session.get("id") or "").strip()
    user = username_for_log(aid)
    rp_cfg = red_packet_cfg()
    result: dict[str, Any] = {
        "account_id": aid,
        "username": user,
        "skipped": False,
        "claimed": False,
        "amount_received": 0,
    }

    if not rp_cfg.get("enabled"):
        result["skipped"] = True
        result["reason"] = "disabled"
        _log_red_packet(user, "bo qua (RED_PACKET_ENABLED=false)")
        return result
    if not rp_cfg.get("on_playwright_token"):
        result["skipped"] = True
        result["reason"] = "on_playwright_token=false"
        _log_red_packet(user, "bo qua (on_playwright_token=false)")
        return result
    if rp_cfg.get("once_per_vn_day") and red_packet_claimed_ok_today(aid):
        result["skipped"] = True
        result["reason"] = "claimed_ok_today"
        _log_red_packet(user, "bo qua — da nhan thanh cong hom nay (VN)")
        return result

    t = timings_from_red_packet_cfg(rp_cfg)
    last_grab: dict[str, Any] = {}
    last_info: dict[str, Any] = {}

    def on_response(response: Any) -> None:
        nonlocal last_grab, last_info
        url = str(response.url or "")
        if "/server/" not in url or "redpacket" not in url.lower():
            return
        path = url.split(".com", 1)[-1].split("?")[0]
        apply_response_tokens(session, response.headers)
        try:
            body = response.text()
        except Exception as e:
            body = str(e)
        parsed = _parse_body_text(body)
        short = path.rsplit("/", 1)[-1]
        if "getredpacketinfo" in path:
            last_info = parsed if isinstance(parsed, dict) else {}
            lst = (last_info.get("data") or {}).get("list") or []
            _log_red_packet(
                user,
                f"API GET {short} status={response.status} list={len(lst)} isNew={(last_info.get('data') or {}).get('isNew')}",
            )
        if "grabredpacket" in path:
            last_grab = parsed if isinstance(parsed, dict) else {}
            code = last_grab.get("code") if last_grab else "?"
            msg = last_grab.get("msg") if last_grab else ""
            _log_red_packet(user, f"API POST {short} status={response.status} code={code} msg={msg}")

    page.on("response", on_response)

    bal_before = get_user_balance(session, refresh=True).get("balance")
    result["balance_before"] = bal_before
    _log_red_packet(user, f"bat dau tren /home/ (balance={bal_before})")

    try:
        page.wait_for_timeout(int(t.load_wait_sec * 1000))
        _log_red_packet(user, "click mua li xi")
        page.mouse.click(700, 450)
        page.wait_for_timeout(int(t.popup_wait_sec * 1000))

        def _step_log(name: str, payload: dict) -> None:
            if name == "click_open":
                _log_red_packet(user, f"bam Mo bao — {payload.get('selector', '')}")
            elif name == "click_duoc_nhan":
                _log_red_packet(user, "bam Duoc nhan (mo lai danh sach)")

        opened = _click_open_packet(page, _step_log)
        if not opened or (last_grab and last_grab.get("code") != 1):
            if _click_duoc_nhan(page, _step_log):
                _click_open_packet(page, _step_log)
        page.wait_for_timeout(int(t.after_open_sec * 1000))
    except Exception as e:
        result["error"] = str(e)
        _log_red_packet(user, f"loi UI: {e}")

    ui_ok = isinstance(last_grab, dict) and last_grab.get("code") == 1
    grab: dict[str, Any] = {
        "ok": ui_ok,
        "code": last_grab.get("code") if last_grab else None,
        "msg": last_grab.get("msg") if last_grab else None,
        "via": "ui",
    }

    if not ui_ok and rp_cfg.get("http_fallback"):
        _log_red_packet(user, "UI chua OK — thu HTTP getredpacketinfo + grabredpacket")
        info = fetch_red_packet_info(session)
        pid = info.get("packet_id")
        if pid:
            _log_red_packet(user, f"HTTP grab id={pid}")
            grab_http = grab_red_packet(session, pid)
            grab_http["via"] = "http"
            _log_red_packet(
                user,
                f"HTTP grab code={grab_http.get('code')} msg={grab_http.get('msg')}",
            )
            if grab_http.get("ok"):
                grab = grab_http
        else:
            _log_red_packet(user, "HTTP getredpacketinfo — khong co list[].id (khong co bao?)")

    bal_after = get_user_balance(session, refresh=True).get("balance")
    result["balance_after"] = bal_after
    result["grab"] = grab
    result["claimed"] = bool(grab.get("ok")) or (
        bal_before is not None
        and bal_after is not None
        and str(bal_before) != str(bal_after)
    )
    result["amount_received"] = amount_received(result)
    result["msg"] = grab.get("msg")

    if result["claimed"]:
        _log_red_packet(
            user,
            f"NHAN DUOC +{result['amount_received']:,} VND | {bal_before} -> {bal_after}",
        )
    else:
        _log_red_packet(
            user,
            f"KHONG nhan duoc | balance {bal_before} -> {bal_after}"
            + (f" | {grab.get('msg')}" if grab.get("msg") else ""),
        )

    if persist and aid:
        persist_session(aid, session)
        _save_claim_log(aid, result)

    return result


def _click_open_packet(page, log_fn) -> bool:
    """Bấm Mở bao lì xì trên popup hoặc sau Được nhận."""
    for sel in ("text=Mở bao lì xì", "text=Mở bao li xi", "button:has-text('Mở bao')"):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=8000):
                page.wait_for_timeout(800)
                loc.click(timeout=10_000)
                log_fn("click_open", {"selector": sel})
                return True
        except Exception as e:
            log_fn("click_open_try", {"selector": sel, "err": str(e)[:120]})
    return False


def _to_int_money(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(float(str(v).replace(",", "").strip()))
    except (ValueError, TypeError):
        return None


def amount_received(result: dict[str, Any]) -> int:
    """Số tiền nhận được — ưu tiên chênh lệch số dư."""
    b0 = _to_int_money(result.get("balance_before"))
    b1 = _to_int_money(result.get("balance_after"))
    if b0 is not None and b1 is not None and b1 > b0:
        return b1 - b0
    grab = result.get("grab") or {}
    data = grab.get("data")
    if isinstance(data, dict):
        for key in ("money", "amount", "receive_money", "bonus", "total_money"):
            v = _to_int_money(data.get(key))
            if v and v > 0:
                return v
    if isinstance(data, (int, float, str)):
        v = _to_int_money(data)
        if v and v > 0:
            return v
    return 0


def resolve_account_ids(target: str) -> list[str]:
    """username, account id (acc16), hoặc all."""
    from xoso66_accounts_db import get_account, get_account_by_username, list_accounts

    t = str(target or "").strip()
    if not t:
        return []
    if t.lower() == "all":
        return [str(r["id"]) for r in list_accounts() if r.get("id")]
    row = get_account_by_username(t)
    if not row:
        row = get_account(t)
    if row and row.get("id"):
        return [str(row["id"])]
    return []


def _safe_print(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def _click_duoc_nhan(page, log_fn) -> bool:
    for sel in ("text=Được nhận", "text=Duoc nhan"):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=5000):
                loc.click(timeout=8000)
                log_fn("click_duoc_nhan", {"selector": sel})
                page.wait_for_timeout(3000)
                return True
        except Exception:
            pass
    return False


def claim_red_packet(
    account_id: str,
    session: dict | None = None,
    *,
    timings: RedPacketTimings | None = None,
    headless: bool = True,
    persist: bool = True,
    verbose: bool = True,
) -> dict[str, Any]:
    """
    Flow hoàn chỉnh trong một phiên browser:
    click mưa → (popup) Mở bao → hoặc Được nhận → Mở bao → HTTP grab dự phòng.
    """
    from xoso66_deposit import apply_response_tokens
    from xoso66_playwright_ctx import _playwright_thread_setup, playwright_proxy
    from xoso66_proxy import ensure_proxy, proxy_log_label, site_host
    from xoso66_session import (
        BASE_URL,
        ensure_session,
        get_user_balance,
        merge_playwright_cookies,
        persist_session,
    )

    t = timings or RedPacketTimings()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    log_path = LOG_DIR / f"{account_id}_{ts}_claim.jsonl"

    if session is None:
        session = ensure_session(account_id)
    user = (session.get("user_info") or {}).get("username") or session.get("username")
    proxy_str = ensure_proxy(session)

    result: dict[str, Any] = {
        "account_id": account_id,
        "username": user,
        "proxy": proxy_log_label(proxy_str),
        "log_path": str(log_path),
        "steps": [],
    }
    bal_before = get_user_balance(session, refresh=True).get("balance")
    result["balance_before"] = bal_before

    last_grab: dict[str, Any] = {}
    last_info: dict[str, Any] = {}

    def _say(msg: str) -> None:
        if verbose:
            print(msg, flush=True)

    def _step(name: str, payload: dict) -> None:
        result["steps"].append({"step": name, **payload})
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), "step": name, **payload}, ensure_ascii=False) + "\n")
        if verbose:
            print(f"[{name}] {json.dumps(payload, ensure_ascii=False)[:450]}", flush=True)

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {"start": True, "user": user, "proxy": result["proxy"], "balance": bal_before},
                ensure_ascii=False,
            )
            + "\n"
        )

    host = site_host(BASE_URL)
    px = playwright_proxy(proxy_str)

    _say(f"\n=== {account_id} ({user}) | login, KHONG getredpacketinfo truoc ===")
    _say(f"proxy={result['proxy']} balance={bal_before}")

    _playwright_thread_setup()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        launch_kw: dict[str, Any] = {"headless": headless, "proxy": px}
        if not headless:
            launch_kw["channel"] = "chrome"
            launch_kw["slow_mo"] = 80
        try:
            browser = p.chromium.launch(**launch_kw)
        except Exception:
            browser = p.chromium.launch(headless=headless, proxy=px)

        ctx_kw: dict[str, Any] = {"viewport": {"width": 1400, "height": 900}}
        if session.get("user_agent"):
            ctx_kw["user_agent"] = session["user_agent"]
        context = browser.new_context(**ctx_kw)
        cookies = [
            {"name": str(n), "value": str(v), "domain": host, "path": "/"}
            for n, v in (session.get("cookies") or {}).items()
            if v is not None
        ]
        if cookies:
            context.add_cookies(cookies)

        page = context.new_page()

        def on_response(response) -> None:
            nonlocal last_grab, last_info
            url = response.url
            if "/server/" not in url:
                return
            if not re.search(r"redpacket|grab", url, re.I):
                return
            path = url.split(".com", 1)[-1].split("?")[0]
            apply_response_tokens(session, response.headers)
            try:
                body = response.text()
            except Exception as e:
                body = str(e)
            parsed = _parse_body_text(body)
            row = {"path": path, "status": response.status, "parsed": parsed}
            if "getredpacketinfo" in path:
                last_info = parsed if isinstance(parsed, dict) else {}
                _step("network_info", row)
            if "grabredpacket" in path:
                last_grab = parsed if isinstance(parsed, dict) else {}
                _step("network_grab", row)

        page.on("response", on_response)

        _say(f"[1] /home/ — doi {t.load_wait_sec}s...")
        page.goto(f"{BASE_URL}/home/", wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(int(t.load_wait_sec * 1000))

        _say("[2] Click mua li xi (1 lan)...")
        page.mouse.click(700, 450)
        _step("click_rain", {"x": 700, "y": 450})

        _say(f"[3] Doi popup {t.popup_wait_sec}s...")
        page.wait_for_timeout(int(t.popup_wait_sec * 1000))

        _say("[4] Bam Mo bao li xi (popup)...")
        opened = _click_open_packet(page, _step)
        result["opened_popup"] = opened

        if not opened or (last_grab and last_grab.get("code") != 1):
            _say("[5] Thu mo lai qua Duoc nhan (= getredpacketinfo UI)...")
            if _click_duoc_nhan(page, _step):
                _click_open_packet(page, _step)

        _say(f"[6] Doi {t.after_open_sec}s sau mo bao...")
        page.wait_for_timeout(int(t.after_open_sec * 1000))

        merge_playwright_cookies(session, context.cookies())
        browser.close()

    ui_ok = isinstance(last_grab, dict) and last_grab.get("code") == 1
    grab: dict[str, Any] = {
        "ok": ui_ok,
        "code": last_grab.get("code") if last_grab else None,
        "msg": last_grab.get("msg") if last_grab else None,
        "via": "ui",
        "raw": last_grab,
    }
    _step("grab_ui", grab)

    if not ui_ok:
        _say("[7] UI chua OK — GET getredpacketinfo + POST grabredpacket (HTTP)...")
        time.sleep(t.after_info_sec)
        info = fetch_red_packet_info(session)
        _step("getredpacketinfo", info)
        result["packet_id"] = info.get("packet_id")
        pid = info.get("packet_id")
        if pid:
            grab_http = grab_red_packet(session, pid)
            grab_http["via"] = "http"
            _step("grabredpacket", grab_http)
            if grab_http.get("ok"):
                grab = grab_http

    if persist:
        persist_session(account_id, session)

    bal_after = get_user_balance(session, refresh=True).get("balance")
    result["balance_after"] = bal_after
    result["grab"] = grab
    result["ok"] = bool(grab.get("ok")) or (
        bal_before is not None
        and bal_after is not None
        and str(bal_before) != str(bal_after)
    )
    result["claimed"] = result["ok"]
    result["amount_received"] = amount_received(result)

    _say(
        f"\nKet qua: claimed={result['claimed']} | "
        f"balance {bal_before} -> {bal_after} | +{result['amount_received']} VND | "
        f"grab code={grab.get('code')} via={grab.get('via')} msg={grab.get('msg')}"
    )
    _say(f"Log: {log_path}")
    return result


def _claim_one_in_batch(
    account_id: str,
    *,
    idx: int,
    total: int,
    timings: RedPacketTimings,
    headless: bool,
) -> dict[str, Any]:
    from xoso66_accounts_db import username_for_log

    user = username_for_log(account_id)
    _safe_print(f"[RUN] {idx}/{total} {user} ({account_id})")
    t0 = time.time()
    try:
        result = claim_red_packet(
            account_id,
            timings=timings,
            headless=headless,
            verbose=False,
        )
        amt = int(result.get("amount_received") or 0)
        claimed = bool(result.get("claimed"))
        status = "CO" if claimed else "KHONG"
        extra = ""
        if not claimed and result.get("grab", {}).get("msg"):
            extra = f" | {result['grab']['msg']}"
        _safe_print(
            f"[DONE] {user} ({account_id}): {status} | +{amt:,} VND | "
            f"{result.get('balance_before')} -> {result.get('balance_after')}{extra} "
            f"({round(time.time() - t0, 1)}s)"
        )
        return {
            "account_id": account_id,
            "username": user,
            "ok": True,
            "claimed": claimed,
            "amount_received": amt,
            "balance_before": result.get("balance_before"),
            "balance_after": result.get("balance_after"),
            "log_path": result.get("log_path"),
            "elapsed_sec": round(time.time() - t0, 1),
            "error": None,
        }
    except Exception as e:
        _safe_print(f"[ERR] {user} ({account_id}): {e}")
        return {
            "account_id": account_id,
            "username": user,
            "ok": False,
            "claimed": False,
            "amount_received": 0,
            "error": str(e),
            "elapsed_sec": round(time.time() - t0, 1),
        }


def run_batch(
    target: str,
    *,
    parallel: int = BATCH_PARALLEL_DEFAULT,
    timings: RedPacketTimings | None = None,
    headless: bool = True,
) -> dict[str, Any]:
    from xoso66_accounts_db import init_db, username_for_log

    init_db()
    account_ids = resolve_account_ids(target)
    if not account_ids:
        raise SystemExit(f"Khong tim thay tai khoan: {target!r}")

    t = timings or RedPacketTimings()
    total = len(account_ids)
    workers = 1 if total == 1 else max(1, min(parallel, total))
    t0 = time.time()

    _safe_print(
        f"\n=== Nhan li xi | {total} nick | song song {workers} ===\n"
    )

    results: list[dict[str, Any]] = []
    if workers == 1:
        for i, aid in enumerate(account_ids, 1):
            results.append(
                _claim_one_in_batch(
                    aid, idx=i, total=total, timings=t, headless=headless
                )
            )
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(
                    _claim_one_in_batch,
                    aid,
                    idx=i,
                    total=total,
                    timings=t,
                    headless=headless,
                ): aid
                for i, aid in enumerate(account_ids, 1)
            }
            for fut in as_completed(futs):
                results.append(fut.result())

    claimed_n = sum(1 for r in results if r.get("claimed"))
    total_amt = sum(int(r.get("amount_received") or 0) for r in results)
    err_n = sum(1 for r in results if r.get("error"))

    _safe_print(f"\n=== Tong ket ({round(time.time() - t0, 1)}s) ===")
    _safe_print(f"Nhan duoc (CO): {claimed_n}/{total}")
    _safe_print(f"Khong nhan (KHONG): {total - claimed_n - err_n}/{total}")
    if err_n:
        _safe_print(f"Loi: {err_n}/{total}")
    _safe_print(f"Tong tien nhan: {total_amt:,} VND")

    for r in sorted(results, key=lambda x: str(x.get("username") or "")):
        u = r.get("username") or username_for_log(r.get("account_id", ""))
        st = "CO" if r.get("claimed") else ("ERR" if r.get("error") else "KHONG")
        _safe_print(
            f"  - {u}: {st} | +{int(r.get('amount_received') or 0):,} VND"
            + (f" | {r['error']}" if r.get("error") else "")
        )

    return {
        "target": target,
        "total": total,
        "claimed_count": claimed_n,
        "total_amount": total_amt,
        "error_count": err_n,
        "elapsed_sec": round(time.time() - t0, 1),
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(
        description="Nhan lì xì — username / acc id / all (song song 5 nick)"
    )
    ap.add_argument(
        "target",
        nargs="?",
        help="username, account id (acc16), hoặc all",
    )
    ap.add_argument("-a", "--account", help="(tương đương target) account id")
    ap.add_argument(
        "--parallel",
        "-j",
        type=int,
        default=BATCH_PARALLEL_DEFAULT,
        help=f"so nick chay cung luc khi all (mac dinh {BATCH_PARALLEL_DEFAULT})",
    )
    ap.add_argument("--show-browser", action="store_true")
    ap.add_argument("--load-wait", type=float, default=5.0)
    ap.add_argument("--popup-wait", type=float, default=15.0)
    ap.add_argument("--after-open", type=float, default=8.0)
    args = ap.parse_args(argv)

    target = (args.target or args.account or "").strip()
    if not target and sys.stdin.isatty():
        target = input("Username hoac all: ").strip()
    if not target:
        ap.print_help()
        return 1

    timings = RedPacketTimings(
        load_wait_sec=args.load_wait,
        popup_wait_sec=args.popup_wait,
        after_open_sec=args.after_open,
    )
    headless = not args.show_browser
    if target.lower() == "all" and args.show_browser:
        print("all: bat buoc headless (bo --show-browser)", flush=True)
        headless = True

    account_ids = resolve_account_ids(target)
    if not account_ids:
        print(f"Khong tim thay: {target}", flush=True)
        return 1

    if len(account_ids) == 1:
        claim_red_packet(
            account_ids[0],
            timings=timings,
            headless=headless,
            verbose=True,
        )
        return 0

    run_batch(
        target,
        parallel=args.parallel,
        timings=timings,
        headless=headless,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
