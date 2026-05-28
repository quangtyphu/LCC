# -*- coding: utf-8 -*-
"""
Kiểm tra khi khởi động (main.py).

Mặc định (giống LC79 user_full_check): **mỗi acc** chạy tuần tự
  balance → ping user-token → refresh HTTP (nếu cần) → retry,
còn **nhiều acc** chạy song song (startup_parallel luồng).

Không quét hết balance 100 acc rồi mới ping 100 acc (chậm gấp đôi).

Chạy riêng:
  python xoso66_startup_checks.py
  python xoso66_startup_checks.py --only balance
  python xoso66_startup_checks.py --only minigame_token
"""

from __future__ import annotations

import argparse
import time
import warnings

warnings.filterwarnings("ignore", message=".*pkg_resources is deprecated.*")
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

from xoso66_accounts_db import (
    STATUS_DANG_CHOI,
    get_account,
    list_accounts,
    list_accounts_by_status,
)


@dataclass
class CheckResult:
    name: str
    status: str  # ok | partial | fail | skipped
    ok_count: int = 0
    fail_count: int = 0
    skip_count: int = 0
    message: str = ""
    details: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: int = 0


def _checks_cfg(cfg: dict) -> dict:
    raw = cfg.get("startup_checks")
    return raw if isinstance(raw, dict) else {}


def _startup_quiet(cfg: dict) -> bool:
    from xoso66_config_util import startup_quiet

    return startup_quiet(cfg)


def _account_ids_for_status(cfg: dict, status_key: str = "balance_status") -> list[str]:
    ccfg = _checks_cfg(cfg)
    status = ccfg.get(status_key)
    if status is None and status_key == "token_status":
        status = ccfg.get("balance_status")
    status = str(status or STATUS_DANG_CHOI)
    if status.strip():
        rows = list_accounts_by_status(status)
    else:
        rows = list_accounts()
    return [str(r["id"]) for r in rows if r.get("id")]


def account_ids_for_startup_checks(
    cfg: dict, status_key: str = "balance_status"
) -> list[str]:
    """
    Danh sách acc cần check khi khởi động.
    scope=status|all: mọi acc theo balance_status / token_status.
    scope=ws_pool (mặc định cũ khi game_worker): chỉ pool WS.
    """
    ccfg = _checks_cfg(cfg)
    override = ccfg.get("account_ids_override")
    if isinstance(override, list) and override:
        return [str(x).strip() for x in override if str(x).strip()]
    scope = str(ccfg.get("scope") or "").strip().lower()
    if scope in ("status", "all"):
        return _account_ids_for_status(cfg, status_key)
    use_ws_pool = scope == "ws_pool" or (
        scope not in ("status", "all") and bool(cfg.get("game_worker_enabled"))
    )
    if use_ws_pool:
        from xoso66_ws_pool import resolve_ws_account_ids, ws_account_count

        try:
            ids = resolve_ws_account_ids(cfg)
        except Exception:
            ids = []
        if ids:
            return ids
        if not _startup_quiet(cfg):
            print(
                "[STARTUP] Chưa cache WS — fallback kiểm tra theo status «Đang Chơi»",
                flush=True,
            )
    return _account_ids_for_status(cfg, status_key)


_PARALLEL_DEFAULTS: dict[str, int] = {
    "startup_parallel": 16,
    "balance_parallel": 16,
    "token_parallel": 16,
    "token_refresh_workers": 8,
}


def _parallel_count(cfg: dict, specific_key: str, n_accounts: int) -> int:
    """Số worker song song — mặc định 8/luồng (không chạy 56 cùng lúc)."""
    ccfg = _checks_cfg(cfg)
    n = max(1, n_accounts)
    cap_raw = int(ccfg.get("parallel_max") or 64)
    cap = n if cap_raw <= 0 else max(1, cap_raw)
    specific = int(ccfg.get(specific_key) or 0)
    if specific > 0:
        return min(specific, n, cap)
    default_w = int(_PARALLEL_DEFAULTS.get(specific_key) or 8)
    return min(n, cap, default_w)


def _fmt_balance_vnd(val: Any) -> str:
    try:
        return f"{int(round(float(val))):,}"
    except (TypeError, ValueError):
        return "?"


def _log_user(d: dict[str, Any]) -> str:
    from xoso66_accounts_db import username_for_log

    return username_for_log(str(d.get("account_id") or ""), d)


def _print_token_line(d: dict[str, Any], *, phase: str = "") -> None:
    user = _log_user(d)
    prefix = f"{phase} " if phase else ""
    if d.get("skipped"):
        print(f"      {prefix}{user}: bỏ qua — {d.get('reason')}", flush=True)
        return
    if d.get("ok"):
        act = d.get("action") or "ok"
        extra = " +refresh" if d.get("refreshed") else ""
        print(f"      {prefix}{user}: OK [{act}{extra}]", flush=True)
        return
    print(f"      {prefix}{user}: FAIL — {d.get('reason')}", flush=True)


def _print_balance_line(d: dict[str, Any]) -> None:
    user = _log_user(d)
    if d.get("skipped"):
        print(f"      {user}: bỏ qua — {d.get('reason')}", flush=True)
        return
    if d.get("ok"):
        path = d.get("path") or "getBalance"
        print(
            f"      {user}: balance: {_fmt_balance_vnd(d.get('balance'))} [{path}]",
            flush=True,
        )
        return
    print(f"      {user}: FAIL — {d.get('reason')}", flush=True)


def _run_parallel(
    fn: Callable[[str], dict[str, Any]],
    account_ids: list[str],
    *,
    workers: int,
    on_each: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, aid): aid for aid in account_ids}
        for fut in as_completed(futs):
            d = fut.result()
            details.append(d)
            if on_each:
                on_each(d)
    details.sort(key=lambda d: str(d.get("account_id") or ""))
    return details


def _summarize_details(details: list[dict[str, Any]], *, elapsed_ms: int) -> CheckResult:
    ok = sum(1 for d in details if d.get("ok"))
    skipped = sum(1 for d in details if d.get("skipped"))
    fail = len(details) - ok - skipped
    if fail == 0 and ok > 0:
        st = "ok"
    elif ok > 0:
        st = "partial"
    elif skipped == len(details):
        st = "skipped"
    else:
        st = "fail"
    return st, ok, fail, skipped


def _check_one_balance(account_id: str, sessions: dict[str, dict], cfg: dict) -> dict[str, Any]:
    """
    Startup: 1× getBalance trước (nhanh). Chỉ ensure_session (login/CF) khi fail.
    Tránh ensure_session → session_is_valid → getBalance lần 2 như trước.
    """
    import copy

    from xoso66_proxy import ensure_proxy, resolve_proxy
    from xoso66_session import ensure_session

    ccfg = _checks_cfg(cfg)
    bal_timeout = int(ccfg.get("balance_timeout_sec") or 12)
    use_refresh = bool(ccfg.get("balance_api_refresh", True))
    relogin_on_fail = bool(ccfg.get("balance_relogin_on_fail", False))
    light = ccfg.get("balance_light", True)

    row = get_account(account_id) or {}
    username = str(row.get("username") or account_id)
    if not resolve_proxy(row):
        return {
            "account_id": account_id,
            "username": username,
            "ok": False,
            "skipped": True,
            "reason": "thiếu proxy",
        }

    def _apply_balance(acc: dict, bal: dict) -> dict[str, Any]:
        if not bal.get("ok"):
            raw = bal.get("raw") if isinstance(bal.get("raw"), dict) else {}
            msg = raw.get("msg") if isinstance(raw, dict) else str(bal.get("raw") or "")[:120]
            return {
                "account_id": account_id,
                "username": username,
                "ok": False,
                "reason": msg or f"http={bal.get('http_status')}",
                "need_cf_refresh": bool(bal.get("need_cf_refresh")),
            }
        money = bal.get("balance")
        if money is not None:
            ui = acc.get("user_info")
            if not isinstance(ui, dict):
                ui = {}
                acc["user_info"] = ui
            ui["money"] = money
        return {
            "account_id": account_id,
            "username": username,
            "ok": True,
            "balance": money,
            "session": acc,
        }

    def _get_bal(acc: dict) -> dict[str, Any]:
        from xoso66_deposit import apply_response_tokens, build_common_headers, get_form_token
        from xoso66_session import GET_BALANCE_PATH, BASE_URL, _merge_response_cookies, _requests_session

        ensure_proxy(acc)
        form_token = str(acc.get("form_token") or "").strip()
        if not form_token and not light:
            form_token = get_form_token(acc)
        elif not form_token:
            return {"ok": False, "reason": "thiếu form_token — worker session sẽ login sau"}
        headers = build_common_headers(
            acc,
            form_token=form_token,
            content_type="application/x-www-form-urlencoded/json",
        )
        params = {"refresh": "1"} if use_refresh else {}
        r = _requests_session(acc).get(
            f"{BASE_URL}{GET_BALANCE_PATH}",
            headers=headers,
            params=params,
            timeout=bal_timeout,
        )
        apply_response_tokens(acc, r.headers)
        _merge_response_cookies(acc, r)
        if r.status_code in (401, 403) or "cloudflare" in (r.text or "")[:500].lower():
            return {"ok": False, "http_status": r.status_code, "need_cf_refresh": True}
        try:
            js = r.json()
        except Exception:
            return {"ok": False, "http_status": r.status_code, "need_cf_refresh": True}
        from xoso66_session import is_session_valid_response

        data = js.get("data") if isinstance(js.get("data"), dict) else {}
        balance = data.get("money") or data.get("balance") or data.get("total_money")
        ok = is_session_valid_response(js, http_status=r.status_code)
        return {"ok": ok, "balance": balance, "raw": js}

    try:
        base = sessions.get(account_id)
        if not base:
            return {
                "account_id": account_id,
                "username": username,
                "ok": False,
                "reason": "không có session trong DB",
            }
        acc = copy.deepcopy(base)
        bal = _get_bal(acc)
        if bal.get("ok"):
            out = _apply_balance(acc, bal)
            out["path"] = "getBalance"
            return out

        if not relogin_on_fail:
            out = _apply_balance(acc, bal)
            out["path"] = "getBalance_only"
            return out

        acc = ensure_session(account_id, force_login=False)
        bal = _get_bal(acc)
        out = _apply_balance(acc, bal)
        out["path"] = "ensure_session+getBalance"
        return out
    except Exception as e:
        return {
            "account_id": account_id,
            "username": username,
            "ok": False,
            "reason": str(e),
        }


def _persist_balance_only(account_id: str, acc: dict) -> None:
    """Chỉ sync balance + user_info — không ghi minigame (tránh đè token khi chạy song song)."""
    from xoso66_accounts_db import update_account

    patch: dict[str, Any] = {"session_json": {}}
    ui = acc.get("user_info")
    if isinstance(ui, dict):
        patch["session_json"]["user_info"] = ui
    for k in ("cookies", "headers", "form_token"):
        if acc.get(k) is not None:
            patch["session_json"][k] = acc[k]
    money = None
    if isinstance(ui, dict):
        money = ui.get("money") or ui.get("total_money")
    if money is not None:
        try:
            patch["balance"] = float(money)
        except (TypeError, ValueError):
            pass
    if patch["session_json"] or "balance" in patch:
        update_account(account_id, patch)


def check_balances(cfg: dict) -> CheckResult:
    """getBalance song song; sync balance DB (1 lần đọc session, ghi sau)."""
    from xoso66_sessions_io import load_sessions

    ccfg = _checks_cfg(cfg)
    status = str(ccfg.get("balance_status") or STATUS_DANG_CHOI)
    account_ids = account_ids_for_startup_checks(cfg, "balance_status")
    if not account_ids:
        return CheckResult(
            name="balance",
            status="skipped",
            message=f"không có acc status={status!r}",
        )

    sessions = load_sessions()
    workers = _parallel_count(cfg, "balance_parallel", len(account_ids))
    print_each = bool(ccfg.get("balance_print_each", True)) and not _startup_quiet(cfg)
    if print_each:
        print(f"  [balance] {len(account_ids)} acc, {workers} luồng (chỉ getBalance, không browser)", flush=True)
    t0 = time.perf_counter()
    details = _run_parallel(
        lambda aid: _check_one_balance(aid, sessions, cfg),
        account_ids,
        workers=workers,
        on_each=_print_balance_line if print_each else None,
    )

    for d in details:
        if d.get("ok") and isinstance(d.get("session"), dict):
            _persist_balance_only(str(d["account_id"]), d["session"])
            d.pop("session", None)

    st, ok, fail, skipped = _summarize_details(details, elapsed_ms=0)
    slow = sum(1 for d in details if d.get("path") == "ensure_session+getBalance")
    fail_only = sum(1 for d in details if d.get("path") == "getBalance_only")
    ms = int((time.perf_counter() - t0) * 1000)
    extra = ""
    if slow:
        extra = f", {slow} acc login/CF"
    elif fail_only:
        extra = f", {fail_only} fail (session worker sẽ sửa)"
    return CheckResult(
        name="balance",
        status=st,
        ok_count=ok,
        fail_count=fail,
        skip_count=skipped,
        message=(
            f"{ok} OK, {fail} lỗi, {skipped} bỏ qua / {len(details)} acc "
            f"({ms}ms, {workers} luồng{extra})"
        ),
        details=details,
        elapsed_ms=ms,
    )


def _minigame_game_params(cfg: dict) -> dict[str, Any]:
    from xoso66_minigame_catalog import game_by_key
    from xoso66_playing_game_store import runtime_token_game_key

    game_key = str(_checks_cfg(cfg).get("token_game_key") or "").strip()
    if not game_key:
        game_key = runtime_token_game_key(cfg)
    g = game_by_key(game_key)
    return {
        "game_key": game_key,
        "gid": int(g["game_id"]),
        "gname": str(g.get("gamename") or "lobby"),
        "sub": str(g.get("sub_game_code") or "dice2"),
        "nav_id": int(g.get("nav_id") or 45),
    }


def _ping_one_minigame_token(
    account_id: str,
    cfg: dict,
    sessions: dict[str, dict],
) -> dict[str, Any]:
    """Chỉ ping (~1s) — không refresh, không ensure_session site."""
    import copy

    from xoso66_minigame_refresh import fetch_ws_token, ping_user_token
    from xoso66_proxy import resolve_proxy
    from xoso66_session import persist_session

    ccfg = _checks_cfg(cfg)
    fetch_ws = bool(
        ccfg.get("token_fetch_ws_on_ping", ccfg.get("token_fetch_ws", False))
    )
    gp = _minigame_game_params(cfg)

    row = get_account(account_id) or {}
    username = str(row.get("username") or account_id)
    if not resolve_proxy(row):
        return {
            "account_id": account_id,
            "username": username,
            "ok": False,
            "skipped": True,
            "reason": "thiếu proxy",
        }
    try:
        base = sessions.get(account_id)
        if not base:
            return {
                "account_id": account_id,
                "username": username,
                "ok": False,
                "needs_refresh": True,
                "reason": "không có session trong DB",
            }
        acc = copy.deepcopy(base)
        ping = ping_user_token(
            acc,
            game_id=gp["gid"],
            gamename=gp["gname"],
            sub_game_code=gp["sub"],
        )
        if ping.get("ok"):
            if fetch_ws:
                fetch_ws_token(acc, game_id=gp["gid"], gamename=gp["gname"])
            persist_session(account_id, acc)
            return {
                "account_id": account_id,
                "username": username,
                "ok": True,
                "action": "ping_ok",
                "refreshed": False,
            }
        return {
            "account_id": account_id,
            "username": username,
            "ok": False,
            "needs_refresh": True,
            "reason": ping.get("msg") or ping.get("reason") or "ping fail",
        }
    except Exception as e:
        return {
            "account_id": account_id,
            "username": username,
            "ok": False,
            "needs_refresh": True,
            "reason": str(e),
        }


def _refresh_one_minigame_token(
    account_id: str,
    cfg: dict,
    sessions: dict[str, dict],
    *,
    force_playwright: bool = False,
) -> dict[str, Any]:
    """HTTP gameurl lấy user-token — Playwright chỉ khi bật config hoặc force_playwright (retry)."""
    import copy

    from xoso66_minigame_http import get_minigame
    from xoso66_minigame_refresh import (
        fetch_ws_token,
        ping_user_token,
        refresh_minigame_cf,
        refresh_user_token_playwright,
        refresh_user_token_via_gameurl,
    )
    from xoso66_proxy import resolve_proxy
    from xoso66_session import persist_session

    ccfg = _checks_cfg(cfg)
    allow_pw = force_playwright or bool(ccfg.get("token_refresh_playwright_on_startup", False))
    cf_pw = force_playwright or bool(ccfg.get("token_cf_playwright_on_startup", False))
    fetch_ws = ccfg.get("token_fetch_ws", True)
    gp = _minigame_game_params(cfg)

    row = get_account(account_id) or {}
    username = str(row.get("username") or account_id)
    if not resolve_proxy(row):
        return {
            "account_id": account_id,
            "username": username,
            "ok": False,
            "skipped": True,
            "reason": "thiếu proxy",
        }

    acc: dict[str, Any] | None = None
    try:
        base = sessions.get(account_id)
        if not base:
            return {
                "account_id": account_id,
                "username": username,
                "ok": False,
                "reason": "không có session trong DB",
            }
        acc = copy.deepcopy(base)
        # gameurl cần form_token site chính — chỉ login nếu thiếu (không persist sớm).
        if not str(acc.get("form_token") or "").strip():
            from xoso66_session import ensure_session

            acc = ensure_session(account_id, force_login=True)
        mg = get_minigame(acc)

        action = "gameurl"
        if not (mg.get("cookies") or {}).get("cf_clearance"):
            refresh_minigame_cf(
                acc,
                game_id=gp["gid"],
                gamename=gp["sub"],
                allow_playwright=cf_pw,
            )
            if (mg.get("cookies") or {}).get("cf_clearance") or mg.get("user_token"):
                persist_session(account_id, acc)
                sessions[account_id] = acc

        rep = refresh_user_token_via_gameurl(
            acc,
            nav_id=gp["nav_id"],
            sub_game_code=gp["sub"],
            islobby=0,
            game_id=gp["gid"],
        )
        if not rep.get("ok") and allow_pw:
            rep = refresh_user_token_playwright(
                acc,
                game_id=gp["gid"],
                gamename=gp["gname"],
                nav_id=gp["nav_id"],
                sub_game_code=gp["sub"],
            )
            action = "playwright" if rep.get("ok") else "gameurl_fail"

        # gameurl có thể lấy được user-token nhưng chưa có cf_clearance mini-game →
        # ping fail; vẫn phải lưu DB (acc16 hay gặp).
        mg = get_minigame(acc)
        if mg.get("user_token"):
            persist_session(account_id, acc)
            sessions[account_id] = acc

        ping2 = ping_user_token(
            acc,
            game_id=gp["gid"],
            gamename=gp["gname"],
            sub_game_code=gp["sub"],
        )
        if not ping2.get("ok") and mg.get("user_token"):
            ping_reason = str(ping2.get("reason") or ping2.get("msg") or "")
            if "cf_clearance" in ping_reason and not (mg.get("cookies") or {}).get("cf_clearance"):
                refresh_minigame_cf(
                    acc,
                    game_id=gp["gid"],
                    gamename=gp["sub"],
                    allow_playwright=True,
                )
                if mg.get("user_token") or (mg.get("cookies") or {}).get("cf_clearance"):
                    persist_session(account_id, acc)
                    sessions[account_id] = acc
                ping2 = ping_user_token(
                    acc,
                    game_id=gp["gid"],
                    gamename=gp["gname"],
                    sub_game_code=gp["sub"],
                )

        if ping2.get("ok"):
            if fetch_ws:
                fetch_ws_token(acc, game_id=gp["gid"], gamename=gp["gname"])
            persist_session(account_id, acc)
            sessions[account_id] = acc
            return {
                "account_id": account_id,
                "username": username,
                "ok": True,
                "action": action,
                "refreshed": True,
            }

        hint = ping2.get("msg") or ping2.get("reason") or rep.get("error") or rep.get("msg")
        if mg.get("user_token") and "cf_clearance" in str(hint):
            hint = f"{hint} (đã lưu user-token DB — thiếu CF mini-game)"
        return {
            "account_id": account_id,
            "username": username,
            "ok": False,
            "action": action,
            "reason": hint,
        }
    except Exception as e:
        return {
            "account_id": account_id,
            "username": username,
            "ok": False,
            "reason": str(e),
        }
    finally:
        if acc is not None and (get_minigame(acc).get("user_token")):
            persist_session(account_id, acc)
            sessions[account_id] = acc


def _print_combined_line(d: dict[str, Any]) -> None:
    user = _log_user(d)
    if d.get("skipped"):
        print(f"      {user}: bỏ qua — {d.get('reason')}", flush=True)
        return
    if d.get("balance_ok"):
        bal = f"balance: {_fmt_balance_vnd(d.get('balance'))}"
    else:
        bal = "balance FAIL"
    tok = d.get("token_action") or ("ping_ok" if d.get("token_ok") else "token FAIL")
    print(f"      {user}: {bal} | {tok}", flush=True)


def _startup_balance_line(d: dict[str, Any]) -> str:
    user = _log_user(d)
    if d.get("skipped"):
        return f"  {user}: bỏ qua — {d.get('reason')}"
    if d.get("balance_ok"):
        bal = _fmt_balance_vnd(d.get("balance"))
    else:
        bal = "?"
    tok = "OK" if d.get("token_ok") else "FAIL"
    return f"  {user}: balance {bal} | token {tok}"


def _print_startup_balance_summary(
    results: list[CheckResult], *, tag: str = "xong"
) -> None:
    """Sau startup combined — liệt kê từng nick + balance (luôn in, kể cả quiet)."""
    details: list[dict[str, Any]] = []
    for r in results:
        if r.name == "startup_combined" and r.details:
            details = list(r.details)
            break
    if not details:
        return
    from xoso66_config_util import main_progress

    main_progress(f"[STARTUP] {tag}:")
    for d in sorted(details, key=lambda x: str(x.get("username") or x.get("account_id") or "")):
        main_progress(_startup_balance_line(d))


def _token_refresh_with_retry(
    account_id: str,
    cfg: dict,
    sessions: dict[str, dict],
) -> dict[str, Any]:
    """Refresh + retry trên cùng acc (không đợi cả đám acc ping xong)."""
    ccfg = _checks_cfg(cfg)
    retry_max = max(0, int(ccfg.get("token_retry_count") or 1))
    last = _refresh_one_minigame_token(account_id, cfg, sessions)
    for attempt in range(retry_max):
        if last.get("ok") or last.get("skipped"):
            break
        use_pw = bool(ccfg.get("token_retry_playwright", False)) and attempt == retry_max - 1
        last = _refresh_one_minigame_token(
            account_id, cfg, sessions, force_playwright=use_pw
        )
    return last


def _minigame_pipeline_one_account(
    account_id: str,
    sessions: dict[str, dict],
    cfg: dict,
) -> dict[str, Any]:
    """Một acc: ping → refresh → retry (tuần tự)."""
    ccfg = _checks_cfg(cfg)
    mode = str(ccfg.get("token_startup_mode") or "ping_then_refresh").strip().lower()
    ping = _ping_one_minigame_token(account_id, cfg, sessions)
    username = str(ping.get("username") or account_id)
    if ping.get("skipped"):
        return ping
    if ping.get("ok") and not ping.get("needs_refresh"):
        return ping
    if mode == "ping_only":
        return ping
    ref = _token_refresh_with_retry(account_id, cfg, sessions)
    ref["username"] = ref.get("username") or username
    return ref


def _startup_pipeline_one_account(
    account_id: str,
    sessions: dict[str, dict],
    cfg: dict,
) -> dict[str, Any]:
    """
    LC79-style: balance → ping token → refresh/retry trên cùng luồng acc.
    100 acc × 3 bước ≈ ceil(100/workers) × (t1+t2+t3), không phải 3×100 tuần tự toàn cục.
    """
    ccfg = _checks_cfg(cfg)
    mode = str(ccfg.get("token_startup_mode") or "ping_then_refresh").strip().lower()

    bal = _check_one_balance(account_id, sessions, cfg)
    username = str(bal.get("username") or account_id)
    if bal.get("skipped"):
        return {
            "account_id": account_id,
            "username": username,
            "skipped": True,
            "reason": bal.get("reason"),
            "balance_ok": False,
            "token_ok": False,
        }
    if bal.get("ok") and isinstance(bal.get("session"), dict):
        _persist_balance_only(account_id, bal["session"])
        sessions[account_id] = bal["session"]

    ping = _ping_one_minigame_token(account_id, cfg, sessions)
    username = str(ping.get("username") or username)
    balance_ok = bool(bal.get("ok"))
    token_ok = bool(ping.get("ok"))
    needs_refresh = bool(ping.get("needs_refresh")) or not balance_ok
    refreshed = False
    token_action = ping.get("action")
    reason = ""

    if not token_ok:
        reason = str(ping.get("reason") or "")
    elif not balance_ok:
        reason = str(bal.get("reason") or "")

    if needs_refresh and mode != "ping_only":
        ref = _token_refresh_with_retry(account_id, cfg, sessions)
        token_ok = bool(ref.get("ok"))
        refreshed = bool(ref.get("refreshed"))
        token_action = ref.get("action") or token_action
        if not token_ok:
            reason = str(ref.get("reason") or reason)

    return {
        "account_id": account_id,
        "username": username,
        "ok": balance_ok and token_ok,
        "balance_ok": balance_ok,
        "balance": bal.get("balance"),
        "token_ok": token_ok,
        "token_action": token_action,
        "needs_refresh": needs_refresh,
        "refreshed": refreshed,
        "reason": reason,
    }


def _run_token_refresh_phase(
    cfg: dict,
    sessions: dict[str, dict],
    ping_details: list[dict[str, Any]],
    account_ids: list[str],
) -> list[dict[str, Any]]:
    """Pha refresh HTTP — chỉ acc ping/balance fail (dùng chung combined + minigame_token)."""
    ccfg = _checks_cfg(cfg)
    mode = str(ccfg.get("token_startup_mode") or "ping_then_refresh").strip().lower()
    print_each = ccfg.get("token_print_each", True)
    retry_max = max(0, int(ccfg.get("token_retry_count") or 1))
    refresh_workers = _parallel_count(cfg, "token_refresh_workers", len(account_ids))

    if mode == "ping_only":
        return ping_details

    need_refresh = [
        str(d["account_id"])
        for d in ping_details
        if d.get("needs_refresh") and d.get("account_id") and not d.get("skipped")
    ]
    merged = {str(d["account_id"]): d for d in ping_details if d.get("account_id")}

    if need_refresh:

        def _on_refresh(d: dict[str, Any]) -> None:
            if print_each:
                _print_token_line(d, phase="refresh")

        print(
            f"  [token] refresh {len(need_refresh)} acc "
            f"(HTTP gameurl, {refresh_workers} luồng)",
            flush=True,
        )
        for d in _run_parallel(
            lambda aid: _refresh_one_minigame_token(aid, cfg, sessions),
            need_refresh,
            workers=refresh_workers,
            on_each=_on_refresh if print_each else None,
        ):
            merged[str(d["account_id"])] = d

    for attempt in range(retry_max):
        failed_ids = [
            aid for aid, d in merged.items() if not d.get("ok") and not d.get("skipped")
        ]
        if not failed_ids:
            break

        def _on_retry(d: dict[str, Any]) -> None:
            if print_each:
                _print_token_line(d, phase=f"retry{attempt + 1}")

        print(
            f"  [token] retry lần {attempt + 1}/{retry_max}: {len(failed_ids)} acc "
            f"({refresh_workers} luồng)",
            flush=True,
        )
        use_pw = bool(ccfg.get("token_retry_playwright", False)) and attempt == retry_max - 1
        if use_pw and print_each:
            print("      (retry cuối: cho phép Playwright nếu HTTP fail)", flush=True)

        for d in _run_parallel(
            lambda aid: _refresh_one_minigame_token(
                aid, cfg, sessions, force_playwright=use_pw
            ),
            failed_ids,
            workers=refresh_workers,
            on_each=_on_retry if print_each else None,
        ):
            merged[str(d["account_id"])] = d

    return [merged[aid] for aid in account_ids if aid in merged]


def check_startup_combined(cfg: dict) -> CheckResult:
    """
    LC79-style: mỗi acc balance→ping→refresh tuần tự; nhiều acc song song.
    """
    from xoso66_sessions_io import load_sessions

    ccfg = _checks_cfg(cfg)
    quiet = _startup_quiet(cfg)
    print_each = bool(ccfg.get("token_print_each", True)) and not quiet
    status = str(ccfg.get("token_status") or ccfg.get("balance_status") or STATUS_DANG_CHOI)
    account_ids = account_ids_for_startup_checks(cfg, "balance_status")
    if not account_ids:
        return CheckResult(
            name="startup_combined",
            status="skipped",
            message=f"không có acc status={status!r}",
        )

    sessions = load_sessions()
    workers = _parallel_count(cfg, "startup_parallel", len(account_ids))
    t0 = time.perf_counter()
    if print_each:
        print(
            f"  [startup] {len(account_ids)} acc × (balance→ping→refresh), "
            f"{workers} luồng song song",
            flush=True,
        )
    final_details = _run_parallel(
        lambda aid: _startup_pipeline_one_account(aid, sessions, cfg),
        account_ids,
        workers=workers,
        on_each=_print_combined_line if print_each else None,
    )
    st, ok, fail, skipped = _summarize_details(final_details, elapsed_ms=0)
    refreshed = sum(1 for d in final_details if d.get("refreshed"))
    ms = int((time.perf_counter() - t0) * 1000)
    retry_max = max(0, int(ccfg.get("token_retry_count") or 1))
    retry_note = f", retry≤{retry_max}" if retry_max else ""
    return CheckResult(
        name="startup_combined",
        status=st,
        ok_count=ok,
        fail_count=fail,
        skip_count=skipped,
        message=(
            f"{ok} OK ({refreshed} refresh{retry_note}), {fail} lỗi, {skipped} bỏ qua "
            f"/ {len(final_details)} acc ({ms}ms, {workers} luồng)"
        ),
        details=final_details,
        elapsed_ms=ms,
    )


def check_minigame_token(cfg: dict) -> CheckResult:
    """Mỗi acc: ping → refresh → retry tuần tự (không ping hết 100 acc rồi mới refresh)."""
    from xoso66_sessions_io import load_sessions

    ccfg = _checks_cfg(cfg)
    print_each = bool(ccfg.get("token_print_each", True)) and not _startup_quiet(cfg)
    retry_max = max(0, int(ccfg.get("token_retry_count") or 1))
    status = str(ccfg.get("token_status") or ccfg.get("balance_status") or STATUS_DANG_CHOI)
    account_ids = account_ids_for_startup_checks(cfg, "token_status")
    if not account_ids:
        return CheckResult(
            name="minigame_token",
            status="skipped",
            message=f"không có acc status={status!r}",
        )

    sessions = load_sessions()
    workers = _parallel_count(cfg, "token_parallel", len(account_ids))
    t0 = time.perf_counter()
    if print_each:
        print(
            f"  [token] {len(account_ids)} acc × (ping→refresh), {workers} luồng song song",
            flush=True,
        )
    final_details = _run_parallel(
        lambda aid: _minigame_pipeline_one_account(aid, sessions, cfg),
        account_ids,
        workers=workers,
        on_each=(lambda d: _print_token_line(d, phase="")) if print_each else None,
    )
    st, ok, fail, skipped = _summarize_details(final_details, elapsed_ms=0)
    refreshed = sum(1 for d in final_details if d.get("refreshed"))
    ms = int((time.perf_counter() - t0) * 1000)
    retry_note = f", retry≤{retry_max}" if retry_max else ""
    return CheckResult(
        name="minigame_token",
        status=st,
        ok_count=ok,
        fail_count=fail,
        skip_count=skipped,
        message=(
            f"{ok} OK ({refreshed} refresh{retry_note}), {fail} lỗi, {skipped} bỏ qua "
            f"/ {len(final_details)} acc ({ms}ms, {workers} luồng)"
        ),
        details=final_details,
        elapsed_ms=ms,
    )


def check_proxy(_cfg: dict) -> CheckResult:
    return CheckResult(
        name="proxy",
        status="skipped",
        message="chưa triển khai",
    )


# Thứ tự chạy khi khởi động — thêm hàm mới vào đây
_CHECK_REGISTRY: list[tuple[str, Callable[[dict], CheckResult]]] = [
    ("balance", check_balances),
    ("minigame_token", check_minigame_token),
    ("startup_combined", check_startup_combined),
    ("proxy", check_proxy),
]


def _enabled_checks(cfg: dict) -> list[tuple[str, Callable[[dict], CheckResult]]]:
    ccfg = _checks_cfg(cfg)
    if not ccfg.get("enabled", True):
        return []
    only = ccfg.get("only")
    if isinstance(only, str) and only.strip():
        only_set = {only.strip()}
    elif isinstance(only, list):
        only_set = {str(x).strip() for x in only if str(x).strip()}
    else:
        only_set = None

    combined = bool(ccfg.get("combined_startup", True))
    balance_on = bool(ccfg.get("balance_enabled", True))
    token_on = bool(ccfg.get("minigame_token_enabled", True))
    if only_set is not None:
        if "startup_combined" in only_set:
            balance_on = token_on = True
        else:
            balance_on = balance_on and "balance" in only_set
            token_on = token_on and "minigame_token" in only_set

    use_combined = combined and balance_on and token_on
    if balance_on and token_on and not combined:
        print(
            "  [startup] combined_startup=false → 2 vòng toàn cục (chậm). "
            "Nên bật combined_startup: true.",
            flush=True,
        )

    out: list[tuple[str, Callable[[dict], CheckResult]]] = []
    if use_combined:
        out.append(("startup_combined", check_startup_combined))
    for name, fn in _CHECK_REGISTRY:
        if name == "startup_combined":
            continue
        if use_combined and name in ("balance", "minigame_token"):
            continue
        if only_set is not None and name not in only_set:
            continue
        key = f"{name}_enabled"
        if key in ccfg and not ccfg.get(key):
            continue
        if name == "proxy" and not ccfg.get("include_placeholders", False):
            continue
        out.append((name, fn))
    return out


def _print_result(r: CheckResult, *, verbose: bool = False) -> None:
    if not verbose and r.status == "ok":
        return
    icon = {"ok": "✓", "partial": "!", "fail": "✗", "skipped": "○"}.get(r.status, "?")
    print(f"  [{icon}] {r.name}: {r.message}", flush=True)
    if not verbose or not r.details:
        return
    for d in r.details:
        if d.get("skipped"):
            print(f"      - {_log_user(d)}: bỏ qua ({d.get('reason')})", flush=True)
        elif d.get("ok"):
            extra = ""
            if d.get("balance") is not None:
                extra = f"balance: {_fmt_balance_vnd(d.get('balance'))}"
            elif d.get("action"):
                ref = " refresh" if d.get("refreshed") else ""
                extra = f"token {d.get('action')}{ref}"
            print(f"      - {_log_user(d)}: {extra}", flush=True)
        else:
            print(f"      - {_log_user(d)}: FAIL {d.get('reason')}", flush=True)


def _warn_minigame_deps(cfg: dict) -> None:
    """Playwright + pproxy cần cho CF mini-game qua proxy SOCKS5 có user/pass (vd. acc16)."""
    ccfg = _checks_cfg(cfg)
    if not ccfg.get("enabled", True):
        return
    only = ccfg.get("only")
    if isinstance(only, str) and only.strip() and "minigame_token" not in only:
        return
    if isinstance(only, list) and only and "minigame_token" not in only:
        return
    if ccfg.get("minigame_token_enabled") is False:
        return
    missing: list[str] = []
    try:
        import playwright  # noqa: F401
    except ImportError:
        missing.append("playwright (pip install playwright && playwright install chromium)")
    try:
        import pproxy  # noqa: F401
    except ImportError:
        missing.append("pproxy (pip install pproxy)")
    if missing:
        print(
            "  [token] Cảnh báo: thiếu "
            + "; ".join(missing)
            + " — acc proxy có auth có thể không lấy được cf_clearance mini-game.",
            flush=True,
        )


def run_startup_checks(cfg: dict, *, verbose: bool | None = None) -> dict[str, Any]:
    """
    Chạy các check đã bật. Trả summary {ok, results: [...]}.
  ok=False nếu có check status=fail (partial vẫn ok=True).
    """
    ccfg = _checks_cfg(cfg)
    if verbose is None:
        verbose = bool(ccfg.get("verbose", False))

    _warn_minigame_deps(cfg)

    checks = _enabled_checks(cfg)
    if not checks:
        print("[STARTUP] Kiểm tra khởi động: tắt (startup_checks.enabled=false)", flush=True)
        return {"ok": True, "results": []}

    quiet = _startup_quiet(cfg)
    if not quiet:
        if any(n == "startup_combined" for n, _ in checks):
            w = _parallel_count(cfg, "startup_parallel", 64)
            print(
                f"[STARTUP] Kiểm tra khởi động (LC79: mỗi acc tuần tự các bước, ~{w} acc song song)...",
                flush=True,
            )
        else:
            print("[STARTUP] Kiểm tra khởi động...", flush=True)
    results: list[CheckResult] = []
    hard_fail = False

    for name, fn in checks:
        try:
            r = fn(cfg)
        except Exception as e:
            r = CheckResult(name=name, status="fail", message=str(e))
        results.append(r)
        _print_result(r, verbose=verbose)
        if r.status == "fail":
            hard_fail = True

    summary = {
        "ok": not hard_fail,
        "results": [
            {
                "name": r.name,
                "status": r.status,
                "ok_count": r.ok_count,
                "fail_count": r.fail_count,
                "skip_count": r.skip_count,
                "message": r.message,
                "elapsed_ms": r.elapsed_ms,
            }
            for r in results
        ],
    }
    tag = "xong" if summary["ok"] else "có lỗi"
    _print_startup_balance_summary(results, tag=tag)
    if not quiet or not summary["ok"]:
        if not any(r.name == "startup_combined" and r.details for r in results):
            print(f"[STARTUP] {tag}.", flush=True)
    try:
        from xoso66_ws_pool import mark_pool_startup_done

        mark_pool_startup_done()
    except Exception:
        pass
    return summary


def run_balance_check_all_db(
    cfg: dict | None = None,
    *,
    parallel: int = 32,
    verbose: bool = True,
) -> CheckResult:
    """getBalance song song — mọi acc trong DB (mỗi acc qua proxy riêng)."""
    from xoso66_config_util import load_config

    patched = dict(cfg or load_config())
    sc = dict(_checks_cfg(patched))
    sc["enabled"] = True
    sc["scope"] = "all"
    sc["balance_status"] = ""
    sc["account_ids_override"] = [
        str(r["id"]) for r in list_accounts() if str(r.get("id") or "").strip()
    ]
    sc["balance_parallel"] = max(1, min(64, int(parallel)))
    sc["balance_enabled"] = True
    sc["combined_startup"] = False
    sc["only"] = "balance"
    sc["balance_print_each"] = bool(verbose)
    patched["startup_checks"] = sc
    n = len(sc["account_ids_override"])
    print(
        f"[BALANCE-ALL] {n} acc trong DB — {sc['balance_parallel']} luồng (proxy từng acc)",
        flush=True,
    )
    return check_balances(patched)


def run_startup_checks_for_playing_accounts(
    cfg: dict, *, verbose: bool | None = None, status: str | None = None
) -> dict[str, Any]:
    """
    Balance + minigame token cho mọi acc CMS theo status (mặc định «Đang Chơi»).
    main.py gọi hàm này khi khởi động (scope=status).
    """
    ccfg = _checks_cfg(cfg)
    st = str(status or ccfg.get("balance_status") or STATUS_DANG_CHOI).strip()
    patched = dict(cfg)
    sc = dict(ccfg)
    sc["scope"] = "status"
    sc["balance_status"] = st
    sc["token_status"] = str(sc.get("token_status") or st)
    patched["startup_checks"] = sc
    if not _startup_quiet(patched):
        n = len(_account_ids_for_status(patched, "balance_status"))
        print(
            f"[STARTUP] Kiểm tra balance + token — {n} acc «{st}» (có proxy)",
            flush=True,
        )
    return run_startup_checks(patched, verbose=verbose)


def run_startup_checks_for_pool(
    cfg: dict, account_ids: list[str], *, verbose: bool | None = None
) -> dict[str, Any]:
    """Balance + user_token cho đúng list pool WS (trước auto nạp)."""
    ids = [str(x).strip() for x in account_ids if str(x).strip()]
    if not ids:
        return {"ok": True, "results": []}
    ccfg = _checks_cfg(cfg)
    if not ccfg.get("enabled", True):
        print("[STARTUP] startup_checks tắt — bỏ qua check trước nạp", flush=True)
        return {"ok": True, "results": []}
    patched = dict(cfg)
    sc = dict(ccfg)
    sc["account_ids_override"] = ids
    patched["startup_checks"] = sc
    if not _startup_quiet(patched):
        print(
            f"[STARTUP] Check balance + user_token cho {len(ids)} acc pool WS (trước nạp)...",
            flush=True,
        )
    return run_startup_checks(patched, verbose=verbose)


def _main() -> int:
    ap = argparse.ArgumentParser(description="Chạy startup checks (không cần main.py)")
    ap.add_argument(
        "--only",
        default="startup_combined",
        help="startup_combined | balance | minigame_token",
    )
    ap.add_argument(
        "--all-db",
        action="store_true",
        help="balance: mọi acc trong DB (không lọc status)",
    )
    ap.add_argument(
        "--parallel",
        type=int,
        default=0,
        help="số luồng song song (balance / combined)",
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="in từng acc")
    args = ap.parse_args()

    from xoso66_accounts_db import init_db
    from xoso66_config_util import load_config

    init_db()
    cfg = load_config()

    if args.all_db:
        par = int(args.parallel) if args.parallel > 0 else 32
        r = run_balance_check_all_db(cfg, parallel=par, verbose=True)
        _print_result(r, verbose=True)
        _print_startup_balance_summary([r], tag="xong")
        if r.details:
            failed = [d for d in r.details if not d.get("ok")]
            if failed:
                print(f"\n[BALANCE-ALL] Lỗi {len(failed)} acc:", flush=True)
                for d in failed:
                    u = d.get("username") or d.get("account_id")
                    print(f"  ✗ {u}: {d.get('error') or d.get('path')}", flush=True)
        return 0 if r.status != "fail" else 1

    c = _checks_cfg(cfg)
    c["enabled"] = True
    only_parts = [x.strip() for x in str(args.only).split(",") if x.strip()]
    if set(only_parts) >= {"balance", "minigame_token"} and "startup_combined" not in only_parts:
        only_parts = ["startup_combined"]
    c["only"] = only_parts if len(only_parts) != 1 else only_parts[0]
    c["combined_startup"] = True
    c["include_placeholders"] = "proxy" in only_parts
    if args.parallel > 0:
        c["balance_parallel"] = args.parallel
        c["startup_parallel"] = args.parallel
        c["token_parallel"] = args.parallel
    if "balance" in only_parts:
        c["balance_enabled"] = True
    if "minigame_token" in only_parts:
        c["minigame_token_enabled"] = True
    if "startup_combined" in only_parts:
        c["balance_enabled"] = True
        c["minigame_token_enabled"] = True

    rep = run_startup_checks(cfg, verbose=args.verbose)
    return 0 if rep.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(_main())
