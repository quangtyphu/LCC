# -*- coding: utf-8 -*-
"""
Nhận lì xì XOSO66 — flow đầy đủ (1 phiên Playwright).

1. Login API — KHÔNG gọi getredpacketinfo trước khi mở web
2. /home/ → đợi load → click mưa (đánh dấu đã biết sự kiện)
3. Đợi popup → bấm 「Mở bao lì xì」 (gọi grabredpacket từ web)
4. Nếu đã tắt popup: bấm 「Được nhận」 (≈ getredpacketinfo) → 「Mở bao lì xì」
5. Network body thường mã hóa (parsed=null) → Vue store dispatch grab trong page
6. HTTP getredpacketinfo + grabredpacket dự phòng (grab HTTP hay lỗi 1057)

Sau login, kiểm tra còn bao: check_red_packet_available(session)
  → GET getredpacketinfo (login user_info KHÔNG có field lì xì).

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

from xoso66_game_domain import resolve_base_url, site_host

BATCH_PARALLEL_DEFAULT = 5
_print_lock = threading.Lock()

DIR = Path(__file__).resolve().parent
LOG_DIR = DIR / "red_packet_logs"

PATH_INFO = "/server/user/getredpacketinfo"
PATH_GRAB = "/server/user/grabredpacket"

# Vue store — sniff tay phamlinhqt 2026-09-21:
#   dispatch("app/grabRedEnvelope", { rid }) → POST /server/user/grabredpacket (cipher)
# HTTP {id} thường lỗi 1057; web dùng field rid.
_VUE_INFO_ACTIONS = (
    "app/getRedEnvelope",
    "user/getredpacketinfo",
    "userCenter/getredpacketinfo",
    "user/getRedPacketInfo",
    "userCenter/getRedPacketInfo",
)
_VUE_GRAB_ACTIONS = (
    "app/grabRedEnvelope",
    "user/grabredpacket",
    "userCenter/grabredpacket",
    "user/grabRedPacket",
    "userCenter/grabRedPacket",
)

# Hardcode — không đọc xoso66_config.json
RED_PACKET_ENABLED = True
RED_PACKET_ON_PLAYWRIGHT_TOKEN = True
RED_PACKET_ONCE_PER_VN_DAY = True
RED_PACKET_LOAD_WAIT_SEC = 3.0
RED_PACKET_POPUP_WAIT_SEC = 12.0
RED_PACKET_AFTER_OPEN_SEC = 6.0
RED_PACKET_HTTP_FALLBACK = True
# Retry khi goto/timeout/Playwright lỗi — dọn browser trước mỗi lần thử lại
RED_PACKET_CLAIM_RETRY_MAX = 3
RED_PACKET_CLAIM_RETRY_DELAY_SEC = 5.0


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
    item = lst[0] if lst and isinstance(lst[0], dict) else {}
    pid = item.get("id")
    if pid is None:
        pid = item.get("rid")
    return {
        "ok": out.get("code") == 1,
        "code": out.get("code"),
        "msg": out.get("msg"),
        "isNew": data.get("isNew"),
        "list": lst,
        "red_notify": data.get("red_notify") or [],
        "packet_id": pid,
        "raw": out,
    }


def grab_red_packet(session: dict, packet_id: int | str) -> dict[str, Any]:
    """POST grabredpacket — thử rid (web) rồi id (bot cũ)."""
    from xoso66_session import post_encrypted

    pid = int(packet_id)
    last: dict[str, Any] = {
        "ok": False,
        "packet_id": pid,
        "raw": {},
    }
    for body in ({"rid": pid}, {"id": pid}):
        _, raw, _ = post_encrypted(session, PATH_GRAB, body)
        out = _parse_api_body(raw)
        last = {
            "ok": out.get("code") == 1,
            "code": out.get("code"),
            "msg": out.get("msg"),
            "data": out.get("data"),
            "packet_id": pid,
            "payload": body,
            "raw": out,
        }
        if last["ok"] or last.get("code") != 1057:
            return last
    return last


def check_red_packet_available(session: dict) -> dict[str, Any]:
    """
    Sau login: gọi getredpacketinfo để biết nick còn lì xì chưa nhận.

    Login /user/login KHÔNG trả field lì xì trong user_info — phải gọi API này.
    available=True khi data.list có phần tử có id (bao đang mở để nhận).
    """
    info = fetch_red_packet_info(session)
    lst = info.get("list") or []
    pid = info.get("packet_id")
    title = ""
    if lst and isinstance(lst[0], dict):
        title = str(lst[0].get("title") or "")
    return {
        "available": bool(pid),
        "packet_id": pid,
        "isNew": info.get("isNew"),
        "count": len(lst) if isinstance(lst, list) else 0,
        "title": title,
        "list": lst,
        "red_notify": info.get("red_notify") or [],
        "ok": bool(info.get("ok")),
        "code": info.get("code"),
        "msg": info.get("msg"),
    }


def _vue_dispatch(page: Any, actions: tuple[str, ...], body: dict[str, Any]) -> dict[str, Any]:
    """Gọi Vuex action trong trang đã login — decrypt sẵn, tránh HTTP 1057."""
    last: dict[str, Any] = {"ok": False, "error": "no_action"}
    for action in actions:
        try:
            js = page.evaluate(
                """async ([action, body]) => {
                    const app = document.querySelector('#app');
                    const vm = app && app.__vue__;
                    if (!vm || !vm.$store || !vm.$store.dispatch) {
                        return { error: 'no_vue_store' };
                    }
                    try {
                        return await vm.$store.dispatch(action, body);
                    } catch (e) {
                        return { code: 0, msg: String(e && e.message || e), action };
                    }
                }""",
                [action, body],
            )
        except Exception as e:
            last = {"ok": False, "error": str(e)[:200], "action": action}
            continue
        if not isinstance(js, dict):
            last = {"ok": False, "raw": js, "action": action}
            continue
        if js.get("error") == "no_vue_store":
            return {"ok": False, "error": "no_vue_store", "action": action}
        code = js.get("code")
        out = {
            "ok": code == 1,
            "code": code,
            "msg": js.get("msg"),
            "data": js.get("data"),
            "action": action,
            "raw": js,
            "via": "vue",
        }
        if code == 1:
            return out
        # action tồn tại nhưng business fail (vd. 1057) — trả luôn để caller xử lý
        if code is not None and "Unknown action" not in str(js.get("msg") or ""):
            return out
        last = out
    return last


def _packet_id_from_info_payload(data: Any) -> int | str | None:
    if not isinstance(data, dict):
        return None
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(inner, dict):
        return None
    lst = inner.get("list") or []
    if lst and isinstance(lst[0], dict):
        if lst[0].get("id") is not None:
            return lst[0].get("id")
        if lst[0].get("rid") is not None:
            return lst[0].get("rid")
    if inner.get("rid") is not None:
        return inner.get("rid")
    return None


def _vue_has_store(page: Any) -> bool:
    try:
        return bool(
            page.evaluate(
                """() => {
                    const app = document.querySelector('#app');
                    const vm = app && app.__vue__;
                    return !!(vm && vm.$store && vm.$store.dispatch);
                }"""
            )
        )
    except Exception:
        return False


def _vue_app_ready(page: Any) -> bool:
    """grabRedEnvelope cần state.app.baseInfo — chỉ có $store thì vẫn crash."""
    try:
        return bool(
            page.evaluate(
                """() => {
                    const app = document.querySelector('#app');
                    const vm = app && app.__vue__;
                    const st = vm && vm.$store && vm.$store.state;
                    const a = st && st.app;
                    return !!(a && a.baseInfo);
                }"""
            )
        )
    except Exception:
        return False


def _wait_vue_store(page: Any, timeout_sec: float = 20.0) -> bool:
    deadline = time.time() + max(1.0, float(timeout_sec))
    while time.time() < deadline:
        if _vue_has_store(page):
            return True
        try:
            page.wait_for_timeout(400)
        except Exception:
            return False
    return _vue_has_store(page)


def _wait_vue_app_ready(page: Any, timeout_sec: float = 12.0) -> bool:
    deadline = time.time() + max(1.0, float(timeout_sec))
    while time.time() < deadline:
        if _vue_app_ready(page):
            return True
        try:
            page.wait_for_timeout(400)
        except Exception:
            return False
    return _vue_app_ready(page)


def _goto_home(page: Any, session: dict, say) -> None:
    """Vào /home/ — commit trước, không chờ domcontentloaded 120s (proxy hay treo)."""
    url = f"{resolve_base_url(session)}/home/"
    say(f"[1] goto {url} (commit, 60s)")
    try:
        page.goto(url, wait_until="commit", timeout=60_000)
    except Exception as e:
        err = str(e).splitlines()[0][:180]
        say(f"[1] commit fail — {err}")
        if _wait_vue_store(page, timeout_sec=8.0):
            say("[1] Vue store co sau commit fail — tiep tuc")
            return
        raise TimeoutError(f"Page.goto /home/ commit fail: {err}") from e
    try:
        page.wait_for_load_state("domcontentloaded", timeout=20_000)
    except Exception as e:
        say(f"[1] domcontentloaded chua xong — {str(e).splitlines()[0][:140]}")
        if _wait_vue_store(page, timeout_sec=8.0):
            say("[1] Vue store co — tiep tuc khong doi DCL")
            return
        say("[1] van tiep tuc (commit da xong, Vue se doi tiep)")


def _vue_grab_packet(page: Any, packet_id: int | str) -> dict[str, Any]:
    """dispatch app/grabRedEnvelope {rid} như nhận tay."""
    pid = int(packet_id)
    out = _vue_dispatch(page, _VUE_GRAB_ACTIONS, {"rid": pid})
    out["payload"] = {"rid": pid}
    if out.get("ok") or out.get("error") == "no_vue_store":
        return out
    if out.get("code") == 1057:
        alt = _vue_dispatch(page, _VUE_GRAB_ACTIONS, {"id": pid})
        alt["payload"] = {"id": pid}
        return alt
    return out


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
                claimed = MAX(red_packet_claim_log.claimed, excluded.claimed),
                amount_vnd = CASE
                    WHEN excluded.claimed = 1 THEN excluded.amount_vnd
                    ELSE red_packet_claim_log.amount_vnd
                END,
                msg = CASE
                    WHEN excluded.claimed = 1 THEN excluded.msg
                    WHEN red_packet_claim_log.claimed = 1 THEN red_packet_claim_log.msg
                    ELSE excluded.msg
                END,
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
        def _step_log(name: str, payload: dict) -> None:
            if name == "click_open":
                _log_red_packet(user, f"bam Mo bao — {payload.get('selector', '')}")
            elif name == "click_duoc_nhan":
                _log_red_packet(user, "bam Duoc nhan (mo lai danh sach)")

        page.wait_for_timeout(int(t.load_wait_sec * 1000))
        _wait_vue_store(page, timeout_sec=max(8.0, t.popup_wait_sec))
        _wait_vue_app_ready(page, timeout_sec=8.0)
        _log_red_packet(user, "click mua li xi")
        page.mouse.click(700, 450)
        t_popup = time.time()
        opened = False
        while time.time() - t_popup < max(2.0, t.popup_wait_sec):
            opened = _click_open_packet(page, _step_log, visible_timeout_ms=400)
            if opened:
                break
            if _vue_app_ready(page) and (time.time() - t_popup) >= 4.0:
                break
            page.wait_for_timeout(600)

        if not opened or (last_grab and last_grab.get("code") != 1):
            if _click_duoc_nhan(page, _step_log, visible_timeout_ms=800):
                opened = _click_open_packet(page, _step_log, visible_timeout_ms=4000) or opened
        if opened:
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

    if not ui_ok:
        pid = None
        if rp_cfg.get("http_fallback"):
            info = fetch_red_packet_info(session)
            pid = info.get("packet_id")
        if pid and _vue_has_store(page):
            _log_red_packet(user, f"UI chua OK — Vue app/grabRedEnvelope rid={pid}")
            if not _vue_app_ready(page):
                _wait_vue_app_ready(page, timeout_sec=8.0)
            grab_vue = _vue_grab_packet(page, pid)
            if not grab_vue.get("ok") and "baseInfo" in str(grab_vue.get("msg") or ""):
                _wait_vue_app_ready(page, timeout_sec=5.0)
                grab_vue = _vue_grab_packet(page, pid)
            _log_red_packet(
                user,
                f"Vue grab action={grab_vue.get('action')} code={grab_vue.get('code')} "
                f"msg={grab_vue.get('msg')} payload={grab_vue.get('payload')}",
            )
            if grab_vue.get("ok"):
                grab = grab_vue
                ui_ok = True
    if not ui_ok and rp_cfg.get("http_fallback"):
        _log_red_packet(user, "UI/Vue chua OK — thu HTTP getredpacketinfo + grabredpacket")
        info = fetch_red_packet_info(session)
        pid = info.get("packet_id")
        if pid:
            _log_red_packet(user, f"HTTP grab rid/id={pid}")
            grab_http = grab_red_packet(session, pid)
            grab_http["via"] = "http"
            _log_red_packet(
                user,
                f"HTTP grab code={grab_http.get('code')} msg={grab_http.get('msg')} "
                f"payload={grab_http.get('payload')}",
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


def _click_open_packet(page, log_fn, *, visible_timeout_ms: int = 800) -> bool:
    """Bấm Mở bao lì xì trên popup hoặc sau Được nhận."""
    for sel in ("text=Mở bao lì xì", "text=Mở bao li xi", "button:has-text('Mở bao')"):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=visible_timeout_ms):
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


def _close_playwright_quietly(
    *,
    page: Any = None,
    context: Any = None,
    browser: Any = None,
) -> None:
    """Đóng page/context/browser trước retry — nuốt mọi lỗi (Windows hay dính socket)."""
    import contextlib

    for obj in (page, context, browser):
        if obj is None:
            continue
        with contextlib.suppress(Exception):
            obj.close()


def _click_duoc_nhan(page, log_fn, *, visible_timeout_ms: int = 800) -> bool:
    for sel in ("text=Được nhận", "text=Duoc nhan"):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=visible_timeout_ms):
                loc.click(timeout=8000)
                log_fn("click_duoc_nhan", {"selector": sel})
                page.wait_for_timeout(1200)
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
    from xoso66_proxy import ensure_proxy, proxy_log_label
    from xoso66_session import (
        ensure_session,
        get_user_balance,
        merge_playwright_cookies,
        persist_session
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
    vue_ok: dict[str, Any] = {}

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

    host = site_host(session)
    px = playwright_proxy(proxy_str)

    _say(f"\n=== {account_id} ({user}) | login, KHONG getredpacketinfo truoc ===")
    _say(f"proxy={result['proxy']} balance={bal_before}")

    _playwright_thread_setup()
    from playwright.sync_api import sync_playwright

    browser = None
    context = None
    page = None
    with sync_playwright() as p:
        try:
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
                    incoming = parsed if isinstance(parsed, dict) else {}
                    _step("network_grab", row)
                    # Body mã hóa → parsed=null; đừng đè kết quả Vue code=1.
                    if incoming.get("code") == 1 or not (
                        isinstance(last_grab, dict) and last_grab.get("code") == 1
                    ):
                        last_grab = incoming

            page.on("response", on_response)

            _say(f"[1] /home/ — doi {t.load_wait_sec}s + Vue store...")
            _goto_home(page, session, _say)
            page.wait_for_timeout(int(t.load_wait_sec * 1000))
            has_vue = _wait_vue_store(page, timeout_sec=max(12.0, t.popup_wait_sec))
            vue_ready = _wait_vue_app_ready(page, timeout_sec=10.0) if has_vue else False
            _say(
                f"[1b] Vue store={'co' if has_vue else 'KHONG'} "
                f"baseInfo={'co' if vue_ready else 'KHONG'}"
            )

            _say("[2] Click mua li xi (1 lan)...")
            page.mouse.click(700, 450)
            _step("click_rain", {"x": 700, "y": 450})

            _say(f"[3-4] Doi popup toi da {t.popup_wait_sec}s — bam ngay khi thay...")
            opened = False
            t_popup = time.time()
            deadline = t_popup + max(2.0, t.popup_wait_sec)
            while time.time() < deadline:
                opened = _click_open_packet(page, _step, visible_timeout_ms=400)
                if opened:
                    break
                if vue_ready and (time.time() - t_popup) >= 4.0:
                    _say("[3-4] Vue ready, khong thay popup — grab som")
                    break
                page.wait_for_timeout(600)
            result["opened_popup"] = opened

            if not opened or (last_grab and last_grab.get("code") != 1):
                _say("[5] Thu mo lai qua Duoc nhan (= getredpacketinfo UI)...")
                if _click_duoc_nhan(page, _step, visible_timeout_ms=800):
                    opened = _click_open_packet(page, _step, visible_timeout_ms=4000) or opened
                    result["opened_popup"] = opened

            if opened:
                _say(f"[6] Doi {t.after_open_sec}s sau mo bao...")
                page.wait_for_timeout(int(t.after_open_sec * 1000))
            else:
                _say("[6] Khong bam duoc Mo bao — bo cho sau mo")
                if has_vue and not vue_ready:
                    vue_ready = _wait_vue_app_ready(page, timeout_sec=6.0)
                    _say(f"[6] baseInfo={'co' if vue_ready else 'KHONG'} sau doi them")

            # Network body thường mã hóa → parsed=null. Dispatch app/grabRedEnvelope {rid}.
            ui_parsed_ok = isinstance(last_grab, dict) and last_grab.get("code") == 1
            if not ui_parsed_ok:
                _say("[6b] UI network chua parse duoc — thu Vue app/grabRedEnvelope {rid}...")
                info_vue = _vue_dispatch(page, _VUE_INFO_ACTIONS, {})
                _step("vue_info", info_vue)
                pid_vue = _packet_id_from_info_payload(info_vue.get("raw") or info_vue)
                if pid_vue is None and isinstance(info_vue.get("data"), (list, dict)):
                    pid_vue = _packet_id_from_info_payload({"data": info_vue.get("data")})
                if pid_vue is None:
                    pid_vue = _packet_id_from_info_payload(info_vue)
                if pid_vue is None:
                    info_http = fetch_red_packet_info(session)
                    _step("getredpacketinfo_for_vue", info_http)
                    pid_vue = info_http.get("packet_id")
                result["packet_id"] = pid_vue
                if pid_vue is not None and _vue_has_store(page):
                    if not _vue_app_ready(page):
                        _say("[6b] baseInfo chua co — doi them roi grab Vue...")
                        _wait_vue_app_ready(page, timeout_sec=8.0)
                    grab_vue = _vue_grab_packet(page, pid_vue)
                    if not grab_vue.get("ok") and "baseInfo" in str(grab_vue.get("msg") or ""):
                        _say("[6b] Vue crash baseInfo — doi 5s roi grab lai...")
                        _wait_vue_app_ready(page, timeout_sec=5.0)
                        grab_vue = _vue_grab_packet(page, pid_vue)
                    grab_vue["packet_id"] = pid_vue
                    _step("vue_grab", grab_vue)
                    if grab_vue.get("ok"):
                        vue_ok = grab_vue
                        raw_ok = grab_vue.get("raw") if isinstance(grab_vue.get("raw"), dict) else grab_vue
                        last_grab = dict(raw_ok) if isinstance(raw_ok, dict) else {"code": 1}
                        last_grab["code"] = last_grab.get("code") or 1
                        last_grab["via"] = "vue"
                elif pid_vue is None:
                    _say("[6b] Khong co list[].id / rid")
                else:
                    _say("[6b] Khong co Vue store — bo qua dispatch")

            merge_playwright_cookies(session, context.cookies())
        finally:
            # Luôn đóng sạch trước khi thoát / raise (để caller retry không dính browser cũ).
            _close_playwright_quietly(page=page, context=context, browser=browser)
            page = context = browser = None

    ui_ok = bool(vue_ok.get("ok")) or (
        isinstance(last_grab, dict) and last_grab.get("code") == 1
    )
    if vue_ok.get("ok"):
        grab = {**vue_ok, "via": "vue", "ok": True}
    else:
        grab = {
            "ok": ui_ok,
            "code": last_grab.get("code") if last_grab else None,
            "msg": last_grab.get("msg") if last_grab else None,
            "via": last_grab.get("via") if isinstance(last_grab, dict) and last_grab.get("via") else "ui",
            "raw": last_grab,
        }
    _step("grab_ui", grab)

    no_packet = False
    if not ui_ok:
        _say("[7] UI/Vue chua OK — GET getredpacketinfo + POST grabredpacket (HTTP)...")
        pid = result.get("packet_id")
        if pid:
            _say(f"[7] dung packet_id={pid} (da co tu getredpacketinfo truoc)")
            info = {"ok": True, "packet_id": pid, "reused": True}
        else:
            time.sleep(t.after_info_sec)
            info = fetch_red_packet_info(session)
            _step("getredpacketinfo", info)
            result["packet_id"] = info.get("packet_id") or result.get("packet_id")
            pid = info.get("packet_id")
        if pid:
            grab_http = grab_red_packet(session, pid)
            grab_http["via"] = "http"
            _step("grabredpacket", grab_http)
            if grab_http.get("ok"):
                grab = grab_http
            else:
                # HTTP hay lỗi 1057 dù list còn bao — giữ soft-fail để batch retry UI.
                grab = {
                    "ok": False,
                    "code": grab_http.get("code"),
                    "msg": grab_http.get("msg"),
                    "via": "http",
                    "packet_id": pid,
                    "raw": grab_http,
                    "still_available": True,
                }
                result["still_available"] = True
        else:
            no_packet = True
            grab = {
                "ok": False,
                "code": info.get("code"),
                "msg": info.get("msg") or "khong co bao li xi",
                "via": "http_info",
                "raw": info,
            }
            _step("no_packet", {"packet_id": None, "info_ok": info.get("ok")})

    if persist:
        persist_session(account_id, session)

    bal_after = get_user_balance(session, refresh=True).get("balance")
    result["balance_after"] = bal_after
    result["grab"] = grab
    result["no_packet"] = no_packet
    b0 = _to_int_money(bal_before)
    b1 = _to_int_money(bal_after)
    balance_up = b0 is not None and b1 is not None and b1 > b0
    result["ok"] = bool(grab.get("ok")) or balance_up
    result["claimed"] = result["ok"]
    result["amount_received"] = amount_received(result)
    result["msg"] = grab.get("msg")
    if no_packet and not result["claimed"]:
        result["msg"] = result.get("msg") or "khong co bao li xi"
    elif grab.get("still_available") and not result["claimed"]:
        result["msg"] = result.get("msg") or "con bao nhung grab fail (thu lai UI)"

    if persist:
        _save_claim_log(account_id, result)

    _say(
        f"\nKet qua: claimed={result['claimed']} | "
        f"balance {bal_before} -> {bal_after} | +{result['amount_received']} VND | "
        f"grab code={grab.get('code')} via={grab.get('via')} msg={grab.get('msg')}"
        + (" | no_packet" if no_packet else "")
        + (" | still_available" if result.get("still_available") else "")
    )
    _say(f"Log: {log_path}")
    return result


def _retry_wait_sec(err: str, default: float) -> float:
    """Cooldown login ~46s thì phải chờ đủ; lỗi mạng chờ lâu hơn 5s."""
    m = re.search(r"(?:cooldown|backoff)\s*~(\d+)", str(err or ""), re.I)
    if m:
        return max(default, float(m.group(1)) + 2.0)
    low = str(err or "").lower()
    if any(
        s in low
        for s in (
            "connection aborted",
            "connection closed",
            "err_connection",
            "timed out",
            "timeout",
            "remote disconnected",
        )
    ):
        return max(default, 15.0)
    return default


def _claim_retry_settings() -> tuple[int, float]:
    """(max_attempts, delay_sec) — ưu tiên auto_red_packet trong config."""
    max_n = RED_PACKET_CLAIM_RETRY_MAX
    delay = RED_PACKET_CLAIM_RETRY_DELAY_SEC
    try:
        from xoso66_config_util import load_config

        raw = load_config().get("auto_red_packet")
        if isinstance(raw, dict):
            if raw.get("claim_retry_max") is not None:
                max_n = int(raw.get("claim_retry_max") or max_n)
            if raw.get("claim_retry_delay_sec") is not None:
                delay = float(raw.get("claim_retry_delay_sec") or delay)
    except Exception:
        pass
    return max(1, max_n), max(0.0, delay)


def _claim_one_in_batch(
    account_id: str,
    *,
    idx: int,
    total: int,
    timings: RedPacketTimings,
    headless: bool,
    verbose: bool = False,
) -> dict[str, Any]:
    from xoso66_accounts_db import username_for_log

    user = username_for_log(account_id)
    if RED_PACKET_ONCE_PER_VN_DAY and red_packet_claimed_ok_today(account_id):
        _safe_print(f"[SKIP] {idx}/{total} {user} ({account_id}): da nhan hom nay")
        return {
            "account_id": account_id,
            "username": user,
            "ok": True,
            "claimed": True,
            "skipped": True,
            "no_packet": False,
            "amount_received": 0,
            "error": None,
            "elapsed_sec": 0,
            "attempts": 0,
        }
    _safe_print(f"[RUN] {idx}/{total} {user} ({account_id})")
    t0 = time.time()
    max_try, delay_s = _claim_retry_settings()
    last: dict[str, Any] | None = None

    for attempt in range(1, max_try + 1):
        try:
            result = claim_red_packet(
                account_id,
                timings=timings,
                headless=headless,
                verbose=verbose,
            )
            amt = int(result.get("amount_received") or 0)
            claimed = bool(result.get("claimed"))
            no_packet = bool(result.get("no_packet"))
            status = "CO" if claimed else ("KHONG (het bao)" if no_packet else "KHONG")
            extra = ""
            if not claimed and result.get("grab", {}).get("msg"):
                extra = f" | {result['grab']['msg']}"
            tag = f" lần {attempt}/{max_try}" if max_try > 1 else ""
            _safe_print(
                f"[DONE] {user} ({account_id}): {status}{tag} | +{amt:,} VND | "
                f"{result.get('balance_before')} -> {result.get('balance_after')}{extra} "
                f"({round(time.time() - t0, 1)}s)"
            )
            last = {
                "account_id": account_id,
                "username": user,
                "ok": True,
                "claimed": claimed,
                "no_packet": no_packet,
                "amount_received": amt,
                "balance_before": result.get("balance_before"),
                "balance_after": result.get("balance_after"),
                "log_path": result.get("log_path"),
                "elapsed_sec": round(time.time() - t0, 1),
                "error": None,
                "attempts": attempt,
            }
            if claimed or no_packet:
                # Có lì xì thì xong; không có bao thì không retry.
                return last
            # KHONG mềm (UI/proxy) — retry.
        except Exception as e:
            _safe_print(
                f"[ERR] {user} ({account_id}): lần {attempt}/{max_try}: {e}"
            )
            last = {
                "account_id": account_id,
                "username": user,
                "ok": False,
                "claimed": False,
                "amount_received": 0,
                "error": str(e),
                "elapsed_sec": round(time.time() - t0, 1),
                "attempts": attempt,
            }

        if attempt >= max_try:
            break
        wait_s = _retry_wait_sec(str(last.get("error") or ""), delay_s)
        _safe_print(
            f"[RETRY] {user} ({account_id}): dọn browser xong — "
            f"thử lại sau {wait_s:.0f}s ({attempt + 1}/{max_try})"
        )
        if wait_s > 0:
            time.sleep(wait_s)

    return last or {
        "account_id": account_id,
        "username": user,
        "ok": False,
        "claimed": False,
        "amount_received": 0,
        "error": "unknown",
        "elapsed_sec": round(time.time() - t0, 1),
        "attempts": max_try,
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
    skipped_n = 0
    if RED_PACKET_ONCE_PER_VN_DAY:
        todo = [aid for aid in account_ids if not red_packet_claimed_ok_today(aid)]
        skipped_n = len(account_ids) - len(todo)
        if skipped_n:
            _safe_print(
                f"Bo qua {skipped_n} nick da nhan hom nay. Con {len(todo)} nick."
            )
        account_ids = todo
    if not account_ids:
        _safe_print("Khong con nick nao can nhan.")
        return {
            "target": target,
            "total": 0,
            "claimed_count": 0,
            "total_amount": 0,
            "error_count": 0,
            "skipped_already": skipped_n,
            "elapsed_sec": 0,
            "results": [],
        }

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

    claimed_n = sum(1 for r in results if r.get("claimed") and not r.get("skipped"))
    total_amt = sum(int(r.get("amount_received") or 0) for r in results)
    err_n = sum(1 for r in results if r.get("error"))
    skipped_run = sum(1 for r in results if r.get("skipped"))

    _safe_print(f"\n=== Tong ket ({round(time.time() - t0, 1)}s) ===")
    _safe_print(f"Nhan duoc (CO): {claimed_n}/{total}")
    _safe_print(f"Khong nhan (KHONG): {total - claimed_n - err_n - skipped_run}/{total}")
    if err_n:
        _safe_print(f"Loi: {err_n}/{total}")
    if skipped_n or skipped_run:
        _safe_print(f"Bo qua (da nhan): {skipped_n + skipped_run}")
    _safe_print(f"Tong tien nhan: {total_amt:,} VND")

    for r in sorted(results, key=lambda x: str(x.get("username") or "")):
        u = r.get("username") or username_for_log(r.get("account_id", ""))
        st = (
            "SKIP"
            if r.get("skipped")
            else ("CO" if r.get("claimed") else ("ERR" if r.get("error") else "KHONG"))
        )
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
        "skipped_already": skipped_n + skipped_run,
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
        _claim_one_in_batch(
            account_ids[0],
            idx=1,
            total=1,
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
