# -*- coding: utf-8 -*-
"""
Lấy / refresh token mini-game (user-token + WS token) — lưu session.minigame + DB.

  python xoso66_minigame_refresh.py -a acc1
  python xoso66_minigame_refresh.py -a acc1 --force
  python xoso66_minigame_refresh.py -a acc1 --ws-only

Luồng:
  1. ensure_session site chính XOSO66
  2. GET /server/thirdgame/gameurl → GET mini-game gameUrl → token trong URL #loginInit
  3. CF cookie mini-game.vip (nếu thiếu)
  4. GET /server/push/getToken → ws_token
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from xoso66_minigame_catalog import GAMES, MERCHANT, game_by_key
from xoso66_minigame_http import (
    GET_TOKEN_PATH,
    MINIGAME_BASE,
    get_minigame,
    lobby_referer,
    merge_minigame_cookies,
    minigame_request,
    ws_url_from_token,
)
from xoso66_deposit import DEFAULT_UA
# Tuổi thọ ước lượng — refresh khi quá hạn hoặc API lỗi auth
USER_TOKEN_MAX_AGE_SEC = int(os.environ.get("XOSO66_MINIGAME_USER_TOKEN_MAX_AGE", "21600"))  # 6h
WS_TOKEN_MAX_AGE_SEC = int(os.environ.get("XOSO66_MINIGAME_WS_TOKEN_MAX_AGE", "600"))  # 10 phút

AUTH_FAIL_CODES = frozenset({0, 401, 403, 1001, 1002, 1004, 1020, 1021})
AUTH_FAIL_MSG = re.compile(
    r"token|phiên|phien|login|đăng nhập|dang nhap|hết hạn|het han|unauthorized",
    re.I,
)


def is_minigame_session_error(code: Any = None, msg: str = "") -> bool:
    """placeOrder / batchRequest — phiên mini-game hết hạn (vd. code 1004)."""
    try:
        if int(code) in AUTH_FAIL_CODES and int(code) != 1:
            return True
    except (TypeError, ValueError):
        pass
    return bool(AUTH_FAIL_MSG.search(str(msg or "")))

MINIGAME_HOST = urlparse(MINIGAME_BASE).netloc
USER_TOKEN_RE = re.compile(r"^[a-f0-9]{32}\.\d{10,}$", re.I)


def _valid_user_token(value: str | None) -> str | None:
    v = (value or "").strip()
    return v if v and USER_TOKEN_RE.match(v) else None


def attach_minigame_token_sniffer(context, on_token) -> None:
    """
    Bắt user-token trên mọi tab/popup (mini-game mở cửa sổ mới — XHR chỉ ở tab đó).
    on_token(token: str) được gọi khi thấy header user-token hợp lệ.
    """
    hooked: set[int] = set()

    def hook_page(page) -> None:
        pid = id(page)
        if pid in hooked:
            return
        hooked.add(pid)

        def on_request(request) -> None:
            if MINIGAME_HOST not in request.url:
                return
            ut = _valid_user_token(request.headers.get("user-token"))
            if ut:
                on_token(ut, request.url)

        page.on("request", on_request)

    context.on("page", hook_page)
    for p in context.pages:
        hook_page(p)


_LAUNCH_PATHS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("/server/game/enterGame", {"game_id": 9, "platform": "pc", "device": "pc"}),
    ("/server/game/play", {"game_id": 9, "id": 9}),
    ("/server/third/enterGame", {"game_id": 9, "merchant": MERCHANT}),
    ("/server/game/getGameUrl", {"game_id": 9, "game_type": 9}),
)


def _is_auth_error(js: dict[str, Any]) -> bool:
    if js.get("code") in AUTH_FAIL_CODES and js.get("code") != 1:
        return True
    msg = str(js.get("msg") or "")
    return bool(AUTH_FAIL_MSG.search(msg))


def _parse_ws_token_issued_at(token: str) -> int | None:
    parts = (token or "").split("_")
    if len(parts) >= 4:
        try:
            return int(parts[3])
        except ValueError:
            pass
    return None


def _parse_user_token_issued_at(token: str) -> float | None:
    """Timestamp nhúng trong token (phần sau dấu chấm), thường là ms."""
    parts = (token or "").split(".", 1)
    if len(parts) != 2:
        return None
    try:
        raw = int(parts[1])
    except ValueError:
        return None
    if raw > 10_000_000_000_000:
        return raw / 1000.0
    if raw > 10_000_000_000:
        return float(raw)
    return None


def _user_token_age_ok(mg: dict) -> bool:
    at = mg.get("user_token_at")
    if not mg.get("user_token") or at is None:
        return False
    try:
        return (time.time() - float(at)) < USER_TOKEN_MAX_AGE_SEC
    except (TypeError, ValueError):
        return False


def _batch_request_ping(
    session: dict,
    *,
    game_id: int = 9,
    gamename: str = "lobby",
    timeout: int = 12,
) -> tuple[int, dict[str, Any]]:
    return minigame_request(
        session,
        "POST",
        "/server/game/batchRequest",
        game_id=game_id,
        gamename=gamename,
        json_body=[
            {"method": "index/init", "params": {}},
            {"method": "game/subgameList", "params": {}},
        ],
        timeout=timeout,
    )


def ping_user_token(
    session: dict,
    *,
    game_id: int = 9,
    gamename: str = "lobby",
) -> dict[str, Any]:
    """
    Kiểm tra nhanh (~1–2s): user-token + cookie CF còn dùng được không.
    Không refresh — chỉ POST batchRequest.
    """
    mg = get_minigame(session)
    if not mg.get("user_token"):
        return {"ok": False, "reason": "thiếu user_token trong session"}
    if not (mg.get("cookies") or {}).get("cf_clearance"):
        return {"ok": False, "reason": "thiếu cf_clearance — chạy refresh_minigame_cf"}
    status, js = _batch_request_ping(session, game_id=game_id, gamename=gamename)
    ok = status == 200 and isinstance(js, dict) and js.get("code") == 1
    return {
        "ok": ok,
        "http_status": status,
        "code": js.get("code") if isinstance(js, dict) else None,
        "msg": js.get("msg") if isinstance(js, dict) else str(js)[:200],
        "auth_error": _is_auth_error(js) if isinstance(js, dict) else False,
    }


def user_token_status(
    session: dict,
    *,
    do_ping: bool = True,
    game_id: int | None = None,
    gamename: str | None = None,
) -> dict[str, Any]:
    """Trạng thái token để log / quyết định refresh nền (không chặn cược)."""
    mg = get_minigame(session)
    tok = str(mg.get("user_token") or "")
    now = time.time()
    at_db = mg.get("user_token_at")
    age_db: float | None = None
    if at_db is not None:
        try:
            age_db = now - float(at_db)
        except (TypeError, ValueError):
            age_db = None
    issued = _parse_user_token_issued_at(tok)
    age_tok = (now - issued) if issued else None
    gid = int(game_id if game_id is not None else mg.get("last_game_id") or 9)
    gname = str(gamename or mg.get("gamename") or "lobby")
    if not tok:
        ping: dict[str, Any] = {"ok": False, "reason": "thiếu token"}
    elif do_ping:
        ping = ping_user_token(session, game_id=gid, gamename=gname)
    else:
        ping = {"ok": None, "skipped": "do_ping=False — chỉ xem tuổi, chưa hỏi server"}
    proactive = float(
        os.environ.get("XOSO66_USER_TOKEN_PROACTIVE_SEC", str(USER_TOKEN_MAX_AGE_SEC // 2))
    )
    return {
        "has_token": bool(tok),
        "token_prefix": tok[:24] + "..." if tok else "",
        "age_db_sec": age_db,
        "age_token_sec": age_tok,
        "max_age_sec": USER_TOKEN_MAX_AGE_SEC,
        "age_ok_db": _user_token_age_ok(mg),
        "ping_ok": ping.get("ok") if ping.get("ok") is not None else None,
        "ping_code": ping.get("code"),
        "ping_msg": ping.get("msg") or ping.get("reason"),
        "needs_refresh": (
            not tok
            or not _user_token_age_ok(mg)
            or (age_db is not None and age_db > proactive)
            or (do_ping and ping.get("ok") is False)
        ),
        "cf_ok": bool((mg.get("cookies") or {}).get("cf_clearance")),
    }


def ensure_user_token_for_bet(
    session: dict,
    account_id: str,
    *,
    game_key: str = "taixiu_dai_loc",
    allow_slow_refresh: bool = False,
) -> tuple[bool, str]:
    """
    Trước placeOrder: ping batchRequest đúng game_id. Thử refresh CF nhanh (curl).
    Playwright chỉ khi allow_slow_refresh=True.
    Trả (ready, message).
    """
    from xoso66_minigame_catalog import game_by_key
    from xoso66_session import persist_session

    g = game_by_key(game_key)
    gid = int(g["game_id"])
    gname = str(g.get("gamename") or "lobby")

    def _ping_ok() -> bool:
        return bool(ping_user_token(session, game_id=gid, gamename=gname).get("ok"))

    if _ping_ok():
        return True, "user-token OK"

    mg = get_minigame(session)
    if not (mg.get("cookies") or {}).get("cf_clearance"):
        refresh_minigame_cf(
            session,
            game_id=gid,
            gamename=gname,
            allow_playwright=False,
            timeout=8,
        )

    if _ping_ok():
        if account_id:
            persist_session(account_id, session)
        return True, "OK sau refresh CF cookie"

    # HTTP gameurl (~3–8s) — không Playwright; xử lý token chết giữa startup và cửa cược.
    nav_id = int(g.get("nav_id") or 45)
    sub_code = str(g.get("sub_game_code") or "dice2")
    gu = refresh_user_token_via_gameurl(
        session,
        nav_id=nav_id,
        sub_game_code=sub_code,
        islobby=0,
        game_id=gid,
    )
    if gu.get("ok") and _ping_ok():
        if account_id:
            persist_session(account_id, session)
        return True, "OK sau refresh gameurl"

    if allow_slow_refresh:
        rep = refresh_minigame_tokens(
            session,
            account_id=account_id,
            game_key=game_key,
            force=True,
            ws_only=False,
        )
        from xoso66_session import persist_session

        persist_session(account_id, session)
        if rep.get("ok") and ping_user_token(session, game_id=gid, gamename=gname).get("ok"):
            return True, "OK sau refresh đầy đủ"
        return False, str(rep.get("error") or rep.get("msg") or "refresh xong vẫn ping fail")

    ping = ping_user_token(session, game_id=gid, gamename=gname)
    st = user_token_status(session, do_ping=False)
    age_db = st.get("age_db_sec")
    age_tok = st.get("age_token_sec")
    if not st.get("has_token"):
        return False, f"thiếu user-token — chạy xoso66_minigame_refresh.py -a {account_id} --force"
    parts = [
        f"user-token không dùng được (ping code={ping.get('code')} {ping.get('msg')})",
    ]
    if age_db is not None:
        parts.append(f"age_db={age_db:.0f}s")
    if age_tok is not None:
        parts.append(f"age_token={age_tok:.0f}s")
    parts.append(
        f"Refresh nền: python xoso66_minigame_refresh.py -a {account_id} --force "
        f"(Playwright ~vài phút — không kịp cửa cược 12–20s)"
    )
    return False, ". ".join(parts)


def _ws_token_age_ok(mg: dict) -> bool:
    token = mg.get("ws_token")
    if not token:
        return False
    issued = mg.get("ws_token_issued_at")
    if issued is None:
        issued = _parse_ws_token_issued_at(str(token))
    if issued is None:
        return False
    try:
        return (time.time() - float(issued)) < WS_TOKEN_MAX_AGE_SEC
    except (TypeError, ValueError):
        return False


def refresh_minigame_cf(
    session: dict,
    *,
    game_id: int = 9,
    gamename: str = "lobby",
    allow_playwright: bool = True,
    timeout: int | None = None,
) -> dict[str, Any]:
    """Lấy cf_clearance / __cf_bm cho domain mini-game.vip (curl_cffi trước; Playwright nếu allow)."""
    from xoso66_proxy import build_proxies, ensure_proxy

    ensure_proxy(session)
    mg = get_minigame(session)
    mg.setdefault("merchant", MERCHANT)
    mg["last_game_id"] = game_id

    url = lobby_referer(game_id=game_id, gamename=gamename)
    ua = session.get("user_agent") or DEFAULT_UA
    steps: list[dict[str, Any]] = []
    req_timeout = int(timeout if timeout is not None else os.environ.get("XOSO66_CF_TIMEOUT", "60"))

    try:
        from curl_cffi import requests as cffi_requests

        r = cffi_requests.get(
            url,
            impersonate=os.environ.get("XOSO66_CF_IMPERSONATE", "chrome131"),
            headers={
                "user-agent": ua,
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "accept-language": "vi-VN,vi;q=0.9,en;q=0.9,en;q=0.8",
            },
            cookies=mg.get("cookies") or {},
            proxies=build_proxies(session["proxy"]),
            timeout=req_timeout,
            allow_redirects=True,
        )
        cookies = dict(mg.get("cookies") or {})
        for name, value in r.cookies.items():
            cookies[name] = value
        mg["cookies"] = cookies
        ok = bool(cookies.get("cf_clearance"))
        steps.append({"method": "curl_cffi", "ok": ok, "http_status": r.status_code})
        if ok:
            return {"ok": True, "steps": steps}
    except ImportError:
        steps.append({"method": "curl_cffi", "ok": False, "error": "chưa cài curl_cffi"})
    except Exception as e:
        steps.append({"method": "curl_cffi", "ok": False, "error": str(e)})

    if not allow_playwright or sys.platform.startswith("win"):
        ok = bool((mg.get("cookies") or {}).get("cf_clearance"))
        if not ok and sys.platform.startswith("win"):
            steps.append(
                {
                    "method": "playwright_minigame",
                    "ok": False,
                    "skipped": "Windows — dùng curl_cffi; không gọi Playwright từ asyncio",
                }
            )
        return {
            "ok": ok,
            "steps": steps,
            "playwright_skipped": True,
        }

    try:
        from xoso66_playwright_ctx import playwright_browser
        from xoso66_session import merge_playwright_cookies
    except ImportError:
        return {"ok": False, "steps": steps, "error": "playwright không khả dụng"}

    captured_token: str | None = None
    headless = os.environ.get("XOSO66_CF_HEADLESS", "1") != "0"

    try:
        with playwright_browser(session, base_url=MINIGAME_BASE, headless=headless) as (
            _p,
            _browser,
            context,
        ):
            page = context.new_page()

            def on_request(request) -> None:
                nonlocal captured_token
                if MINIGAME_HOST not in request.url:
                    return
                ut = request.headers.get("user-token")
                if ut:
                    captured_token = ut

            page.on("request", on_request)
            page.goto(url, wait_until="domcontentloaded", timeout=90_000)
            try:
                page.wait_for_load_state("networkidle", timeout=25_000)
            except Exception:
                page.wait_for_timeout(8_000)

            merge_playwright_cookies(session, context.cookies())
            cookies = dict(mg.get("cookies") or {})
            for c in context.cookies():
                if isinstance(c, dict) and c.get("name"):
                    cookies[str(c["name"])] = str(c.get("value") or "")
            mg["cookies"] = cookies

            if captured_token:
                mg["user_token"] = captured_token
                mg["user_token_at"] = time.time()
    except NotImplementedError as e:
        err = (
            f"Playwright subprocess trên Windows: {e} "
            "(thử restart main.py; hoặc cài curl_cffi để lấy CF không cần PW)"
        )
        steps.append({"method": "playwright_minigame", "ok": False, "error": err})
        print(f"[CF] Playwright lỗi — {err}", flush=True)
        return {"ok": bool((mg.get("cookies") or {}).get("cf_clearance")), "steps": steps}
    except Exception as e:
        steps.append({"method": "playwright_minigame", "ok": False, "error": str(e)})
        print(f"[CF] Playwright lỗi — {e}", flush=True)
        return {"ok": bool((mg.get("cookies") or {}).get("cf_clearance")), "steps": steps}

    ok = bool((mg.get("cookies") or {}).get("cf_clearance"))
    steps.append(
        {
            "method": "playwright_minigame",
            "ok": ok,
            "captured_user_token": bool(captured_token),
        }
    )
    return {"ok": ok, "steps": steps, "captured_user_token": bool(captured_token)}


def _extract_url_from_data(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    for key in ("url", "game_url", "gameUrl", "link", "jump_url", "jumpUrl", "data"):
        v = data.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return v
        if isinstance(v, dict):
            inner = _extract_url_from_data(v)
            if inner:
                return inner
    return None


def _try_launch_from_main_http(session: dict, *, game_id: int) -> dict[str, Any]:
    """Thử API site chính trả URL mini-game (nếu có)."""
    from xoso66_session import post_encrypted

    for path, body in _LAUNCH_PATHS:
        payload = dict(body)
        payload["game_id"] = game_id
        try:
            status, data, _ = post_encrypted(session, path, payload)
        except Exception as e:
            continue
        if status != 200 or not isinstance(data, dict) or data.get("code") != 1:
            continue
        url = _extract_url_from_data(data.get("data"))
        if url and MINIGAME_HOST in url:
            return {"ok": True, "path": path, "url": url}
    return {"ok": False, "error": "không tìm thấy launch URL trên site chính"}


def _token_from_login_init_url(url: str) -> str | None:
    """Parse #/?loginInit=1&data=base64({"token":"..."})."""
    frag = urlparse(url).fragment or ""
    if "data=" not in frag:
        return None
    data_b64 = unquote(frag.split("data=", 1)[1].split("&")[0])
    pad = "=" * (-len(data_b64) % 4)
    try:
        obj = json.loads(base64.b64decode(data_b64 + pad))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    return _valid_user_token(str(obj.get("token") or ""))


def _apply_user_token_from_url(url: str, mg: dict) -> bool:
    tok = _token_from_login_init_url(url)
    if tok:
        mg["user_token"] = tok
        mg["user_token_at"] = time.time()
        return True
    parsed = urlparse(url)
    q = parse_qs(parsed.query)
    for key in ("user-token", "user_token", "token", "usertoken"):
        vals = q.get(key)
        if vals and vals[0]:
            ut = _valid_user_token(vals[0].strip())
            if ut:
                mg["user_token"] = ut
                mg["user_token_at"] = time.time()
                return True
    frag = parse_qs(parsed.fragment)
    for key in ("user-token", "user_token", "token"):
        vals = frag.get(key)
        if vals and vals[0]:
            ut = _valid_user_token(vals[0].strip())
            if ut:
                mg["user_token"] = ut
                mg["user_token_at"] = time.time()
                return True
    return False


def refresh_user_token_via_gameurl(
    session: dict,
    *,
    nav_id: int = 45,
    sub_game_code: str = "dice2",
    islobby: int = 0,
    game_id: int = 9,
) -> dict[str, Any]:
    """
  Launch chính thức (site XOSO66):
    GET /server/thirdgame/gameurl?nav_id=&sub_game_code=&islobby=
    → GET url mini-game .../game/gameUrl?params=...&key=...
    → redirect #/?loginInit=1&data=base64({"token": user-token})
    """
    import requests

    from xoso66_deposit import build_common_headers, get_form_token
    from xoso66_proxy import apply_requests_proxy, ensure_proxy
    from xoso66_session import BASE_URL, _requests_session

    mg = get_minigame(session)
    mg["last_game_id"] = game_id
    mg["gamename"] = sub_game_code

    if not (mg.get("cookies") or {}).get("cf_clearance"):
        refresh_minigame_cf(session, game_id=game_id, gamename=sub_game_code)

    ensure_proxy(session)
    form_token = get_form_token(session)
    if not form_token:
        return {"ok": False, "method": "gameurl", "error": "thiếu form_token site chính"}

    headers = build_common_headers(
        session,
        form_token=form_token,
        content_type="application/x-www-form-urlencoded/json",
    )
    params = {"nav_id": nav_id, "sub_game_code": sub_game_code, "islobby": islobby}
    r = _requests_session(session).get(
        f"{BASE_URL}/server/thirdgame/gameurl",
        headers=headers,
        params=params,
        timeout=45,
    )
    try:
        js = r.json()
    except Exception:
        return {"ok": False, "method": "gameurl", "error": "gameurl response không phải JSON"}

    if js.get("code") != 1:
        return {
            "ok": False,
            "method": "gameurl",
            "code": js.get("code"),
            "msg": js.get("msg"),
        }

    launch_url = _extract_url_from_data(js.get("data"))
    if not launch_url or MINIGAME_HOST not in launch_url:
        return {"ok": False, "method": "gameurl", "error": "không có url mini-game trong data"}

    http = requests.Session()
    apply_requests_proxy(http, session["proxy"])
    r2 = http.get(
        launch_url,
        headers={
            "user-agent": session.get("user_agent") or DEFAULT_UA,
            "accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        },
        cookies=dict(mg.get("cookies") or {}),
        allow_redirects=True,
        timeout=60,
    )
    merge_minigame_cookies(mg, r2)

    tok = _token_from_login_init_url(r2.url)
    if not tok:
        _apply_user_token_from_url(r2.url, mg)
        tok = _valid_user_token(mg.get("user_token"))

    if not tok:
        return {
            "ok": False,
            "method": "gameurl",
            "error": "không parse token từ redirect loginInit",
            "final_url": (r2.url or "")[:160],
        }

    mg["user_token"] = tok
    mg["user_token_at"] = time.time()
    mg["thirdgame_launch_url"] = launch_url

    status, batch_js = minigame_request(
        session,
        "POST",
        "/server/game/batchRequest",
        game_id=game_id,
        gamename=sub_game_code,
        json_body=[
            {"method": "index/init", "params": {}},
            {"method": "game/subgameList", "params": {}},
        ],
    )
    if batch_js.get("code") != 1:
        return {
            "ok": False,
            "method": "gameurl",
            "error": "batchRequest sau gameurl thất bại",
            "batch_code": batch_js.get("code"),
            "batch_msg": batch_js.get("msg"),
            "user_token": tok[:24] + "...",
        }

    return {
        "ok": True,
        "method": "gameurl",
        "user_token": tok[:24] + "...",
        "nav_id": nav_id,
        "sub_game_code": sub_game_code,
    }


def refresh_user_token_via_form_token(
    session: dict,
    *,
    game_id: int = 9,
    gamename: str = "dice_md5",
) -> dict[str, Any]:
    """
    SSO mini-game: mở mini-game.vip?merchant=bet66&game_id=...&token={form_token site chính}.
    Header user-token xuất hiện trên request API mini-game.
    """
    from xoso66_deposit import get_form_token
    from xoso66_playwright_ctx import playwright_browser

    mg = get_minigame(session)
    captured: str | None = None
    form_token = get_form_token(session)
    if not form_token:
        return {"ok": False, "error": "thiếu form_token site chính"}

    sso_url = (
        f"{MINIGAME_BASE}/?merchant={MERCHANT}&game_id={game_id}"
        f"&x-device=pc&token={form_token}"
    )
    headless = os.environ.get("XOSO66_CF_HEADLESS", "1") != "0"

    try:
        with playwright_browser(session, base_url=MINIGAME_BASE, headless=headless) as (
            _p,
            _browser,
            context,
        ):
            page = context.new_page()

            def on_request(request) -> None:
                nonlocal captured
                ut = request.headers.get("user-token")
                if ut:
                    captured = ut

            page.on("request", on_request)
            page.goto(sso_url, wait_until="domcontentloaded", timeout=90_000)
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                page.wait_for_timeout(8_000)

            if captured:
                mg["user_token"] = captured
                mg["user_token_at"] = time.time()

            cookies = dict(mg.get("cookies") or {})
            for c in context.cookies():
                if isinstance(c, dict) and c.get("name"):
                    cookies[str(c["name"])] = str(c.get("value") or "")
            mg["cookies"] = cookies
    except Exception as e:
        return {"ok": False, "method": "form_token_sso", "error": str(e)}

    return {
        "ok": bool(mg.get("user_token")),
        "method": "form_token_sso",
        "sso_url": sso_url[:80] + "...",
    }


def import_user_token(session: dict, user_token: str) -> dict[str, Any]:
    """Gán user-token (từ curl DevTools) và kiểm tra batchRequest."""
    token = (user_token or "").strip()
    if not _valid_user_token(token):
        return {"ok": False, "error": "user-token không đúng dạng (32 hex + . + timestamp)"}
    if not (get_minigame(session).get("cookies") or {}).get("cf_clearance"):
        refresh_minigame_cf(session, game_id=9)
    mg = get_minigame(session)
    mg["user_token"] = token
    mg["user_token_at"] = time.time()
    status, js = minigame_request(
        session,
        "POST",
        "/server/game/batchRequest",
        game_id=int(mg.get("last_game_id") or 9),
        gamename=str(mg.get("gamename") or "dice_md5"),
        json_body=[
            {"method": "index/init", "params": {}},
            {"method": "game/subgameList", "params": {}},
        ],
    )
    ok = status == 200 and js.get("code") == 1
    return {
        "ok": ok,
        "method": "import",
        "http_status": status,
        "code": js.get("code"),
        "msg": js.get("msg"),
        "user_token": token[:24] + "...",
    }


def refresh_user_token_interactive(
    session: dict,
    *,
    game_id: int = 9,
    wait_sec: int = 180,
) -> dict[str, Any]:
    """
    Mở Chrome (headed): click icon mini-game trên XOSO66 → tab mini-game.vip mới.
    Script nghe XHR trên mọi tab/popup và lưu user-token.
    """
    from xoso66_playwright_ctx import playwright_browser
    from xoso66_session import BASE_URL, merge_playwright_cookies

    mg = get_minigame(session)
    captured: str | None = None
    capture_from: str | None = None
    prev = os.environ.get("XOSO66_CF_HEADLESS")
    os.environ["XOSO66_CF_HEADLESS"] = "0"

    def on_token(ut: str, url: str) -> None:
        nonlocal captured, capture_from
        captured = ut
        capture_from = url

    try:
        with playwright_browser(session, base_url=BASE_URL, headless=False) as (
            _p,
            _browser,
            context,
        ):
            attach_minigame_token_sniffer(context, on_token)
            page = context.new_page()
            page.goto(f"{BASE_URL}/home/", wait_until="domcontentloaded", timeout=120_000)
            print(
                f"[interactive] Trong {wait_sec}s: click icon mini-game "
                f"(tab mới mini-game.vip) — script bắt user-token từ XHR tab đó.",
                flush=True,
            )
            page.wait_for_timeout(wait_sec * 1000)
            merge_playwright_cookies(session, context.cookies())
    finally:
        if prev is None:
            os.environ.pop("XOSO66_CF_HEADLESS", None)
        else:
            os.environ["XOSO66_CF_HEADLESS"] = prev

    if not captured:
        return {
            "ok": False,
            "method": "interactive",
            "error": "không bắt được user-token — hãy mở game đến khi thấy batchRequest trong Network",
        }
    mg["user_token"] = captured
    mg["user_token_at"] = time.time()
    return {
        "ok": True,
        "method": "interactive",
        "user_token": captured[:24] + "...",
        "from_url": capture_from,
    }


def refresh_user_token_playwright(
    session: dict,
    *,
    game_id: int = 9,
    gamename: str = "dice_md5",
    nav_id: int = 45,
    sub_game_code: str = "dice2",
) -> dict[str, Any]:
    """gameurl HTTP → SSO form_token → Playwright fallback."""
    via_gu = refresh_user_token_via_gameurl(
        session,
        nav_id=nav_id,
        sub_game_code=sub_game_code,
        islobby=0,
        game_id=game_id,
    )
    if via_gu.get("ok"):
        return via_gu

    via_ft = refresh_user_token_via_form_token(session, game_id=game_id, gamename=gamename)
    if via_ft.get("ok"):
        return via_ft

    from xoso66_playwright_ctx import playwright_browser
    from xoso66_session import merge_playwright_cookies

    mg = get_minigame(session)
    captured: str | None = None
    steps: list[dict[str, Any]] = [via_gu, via_ft]
    headless = os.environ.get("XOSO66_CF_HEADLESS", "1") != "0"

    launch = _try_launch_from_main_http(session, game_id=game_id)
    steps.append({"step": "launch_http", **launch})

    urls_to_open: list[str] = []
    if launch.get("ok") and launch.get("url"):
        urls_to_open.append(str(launch["url"]))
    urls_to_open.append(lobby_referer(game_id=game_id, gamename=gamename))

    from xoso66_session import BASE_URL as MAIN_BASE

    try:
        with playwright_browser(session, base_url=MAIN_BASE, headless=headless) as (
            _p,
            _browser,
            context,
        ):
            def on_token(ut: str, url: str) -> None:
                nonlocal captured
                captured = ut
                mg["user_token"] = ut
                mg["user_token_at"] = time.time()

            attach_minigame_token_sniffer(context, on_token)
            page = context.new_page()

            def on_request(request) -> None:
                if MINIGAME_HOST in request.url:
                    _apply_user_token_from_url(request.url, mg)

            page.on("request", on_request)

            page.goto(f"{MAIN_BASE}/home/", wait_until="domcontentloaded", timeout=90_000)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                page.wait_for_timeout(5_000)

            for open_url in urls_to_open:
                if captured and mg.get("user_token"):
                    break
                try:
                    page.goto(open_url, wait_until="domcontentloaded", timeout=90_000)
                    _apply_user_token_from_url(page.url, mg)
                    try:
                        page.wait_for_load_state("networkidle", timeout=20_000)
                    except Exception:
                        page.wait_for_timeout(8_000)
                except Exception as e:
                    steps.append({"step": "goto", "url": open_url[:120], "error": str(e)})

            if captured:
                mg["user_token"] = captured
                mg["user_token_at"] = time.time()

            merge_playwright_cookies(session, context.cookies())
    except Exception as e:
        return {"ok": False, "steps": steps, "error": str(e)}

    if not (mg.get("cookies") or {}).get("cf_clearance"):
        cf = refresh_minigame_cf(session, game_id=game_id, gamename=gamename)
        steps.append({"step": "minigame_cf", **cf})

    ok = bool(mg.get("user_token"))
    return {"ok": ok, "steps": steps, "user_token": (mg.get("user_token") or "")[:20] + "..." if ok else None}


def fetch_ws_token(
    session: dict,
    *,
    game_id: int = 9,
    gamename: str = "dice_md5",
) -> dict[str, Any]:
    """GET /server/push/getToken — cần user-token + cookie mini-game."""
    mg = get_minigame(session)
    if not mg.get("user_token"):
        return {"ok": False, "error": "thiếu user-token"}

    status, js = minigame_request(
        session,
        "GET",
        GET_TOKEN_PATH,
        game_id=game_id,
        gamename=gamename,
    )
    if status in (401, 403) or _is_auth_error(js):
        return {
            "ok": False,
            "http_status": status,
            "code": js.get("code"),
            "msg": js.get("msg"),
            "need_user_token_refresh": True,
        }

    if js.get("code") != 1:
        return {
            "ok": False,
            "http_status": status,
            "code": js.get("code"),
            "msg": js.get("msg"),
        }

    data = js.get("data") if isinstance(js.get("data"), dict) else {}
    token = str(data.get("token") or "").strip()
    if not token:
        return {"ok": False, "error": "response không có data.token", "raw": js}

    issued = _parse_ws_token_issued_at(token) or int(js.get("time") or time.time())
    mg["ws_token"] = token
    mg["ws_token_issued_at"] = issued
    mg["ws_url"] = ws_url_from_token(token)
    mg["ws_token_at"] = time.time()

    return {
        "ok": True,
        "ws_token": token[:24] + "...",
        "ws_url": mg["ws_url"],
        "issued_at": issued,
    }


def refresh_minigame_tokens(
    session: dict,
    *,
    account_id: str | None = None,
    game_key: str = "taixiu_dai_loc",
    force: bool = False,
    ws_only: bool = False,
) -> dict[str, Any]:
    """
    Refresh đầy đủ mini-game tokens; persist DB nếu có account_id.
    """
    from xoso66_session import ensure_session, persist_session

    aid = str(account_id or session.get("id") or "").strip()
    if not aid:
        raise ValueError("cần account_id")
    caller = session
    session = ensure_session(aid)

    g = game_by_key(game_key)
    game_id = int(g["game_id"])
    sub_game_code = str(g.get("sub_game_code") or "dice2")
    nav_id = int(g.get("nav_id") or 45)
    gamename = str(g.get("gamename") or sub_game_code)

    mg = get_minigame(session)
    mg["merchant"] = MERCHANT
    mg["last_game_id"] = game_id
    report: dict[str, Any] = {"ok": False, "game_key": game_key, "game_id": game_id}

    if not ws_only:
        if force or not (mg.get("cookies") or {}).get("cf_clearance"):
            report["cf"] = refresh_minigame_cf(session, game_id=game_id, gamename=sub_game_code)

        if force or not _user_token_age_ok(mg):
            report["user_token"] = refresh_user_token_playwright(
                session,
                game_id=game_id,
                gamename=gamename,
                nav_id=nav_id,
                sub_game_code=sub_game_code,
            )
            if not report["user_token"].get("ok"):
                report["ok"] = False
                report["error"] = "không lấy được user-token"
                if account_id:
                    persist_session(account_id, session)
                return report

    ws_gamename = str(mg.get("gamename") or sub_game_code)
    ws_report = fetch_ws_token(session, game_id=game_id, gamename=ws_gamename)
    report["ws_token"] = ws_report

    if not ws_report.get("ok") and ws_report.get("need_user_token_refresh") and not ws_only:
        report["user_token_retry"] = refresh_user_token_playwright(
            session,
            game_id=game_id,
            gamename=gamename,
            nav_id=nav_id,
            sub_game_code=sub_game_code,
        )
        ws_report = fetch_ws_token(session, game_id=game_id, gamename=gamename)
        report["ws_token"] = ws_report

    report["ok"] = bool(ws_report.get("ok") and mg.get("user_token"))
    report["minigame"] = {
        "has_user_token": bool(mg.get("user_token")),
        "has_ws_token": bool(mg.get("ws_token")),
        "ws_url": mg.get("ws_url"),
        "balance_hint": mg.get("balance"),
    }

    if account_id:
        persist_session(account_id, session)
    if session is not caller:
        from xoso66_sessions_io import apply_session_merge

        apply_session_merge(caller, session)
    return report


def ensure_minigame_tokens(
    session: dict,
    *,
    account_id: str | None = None,
    game_key: str = "taixiu_dai_loc",
    force: bool = False,
) -> dict[str, Any]:
    """Trả minigame dict; refresh nếu thiếu / hết hạn."""
    mg = get_minigame(session)
    need_user = force or not _user_token_age_ok(mg)
    need_ws = force or not _ws_token_age_ok(mg)
    if need_user or need_ws:
        rep = refresh_minigame_tokens(
            session,
            account_id=account_id or session.get("id"),
            game_key=game_key,
            force=force,
            ws_only=not need_user and need_ws,
        )
        if not rep.get("ok"):
            raise RuntimeError(rep.get("error") or rep.get("ws_token") or "refresh minigame thất bại")
    return mg


def _main() -> int:
    ap = argparse.ArgumentParser(description="Refresh token mini-game XOSO66")
    ap.add_argument("-a", "--account", required=True, help="account id (acc1)")
    ap.add_argument("--force", action="store_true", help="bắt buộc lấy lại user-token + ws")
    ap.add_argument(
        "--check",
        action="store_true",
        help="ping batchRequest + tuổi token (~1–3s, không refresh)",
    )
    ap.add_argument(
        "--ping",
        action="store_true",
        help="chỉ ping server: in OK/FAIL một dòng (~1–3s)",
    )
    ap.add_argument(
        "--check-local",
        action="store_true",
        help="chỉ tuổi token trong DB (tức thì, không chắc server còn chấp nhận)",
    )
    ap.add_argument("--ws-only", action="store_true", help="chỉ gọi getToken (đã có user-token)")
    ap.add_argument(
        "--import-token",
        metavar="USER_TOKEN",
        help="dán user-token từ curl (header user-token khi đang chơi)",
    )
    ap.add_argument(
        "--interactive",
        action="store_true",
        help="mở browser, bạn click game, script bắt user-token (~3 phút)",
    )
    ap.add_argument(
        "--game",
        default="taixiu_dai_loc",
        help=f"game key trong catalog: {', '.join(GAMES)}",
    )
    args = ap.parse_args()

    from xoso66_session import ensure_session, persist_session

    acc = ensure_session(args.account)
    g = game_by_key(args.game)
    gid = int(g["game_id"])
    gname = str(g.get("gamename") or "lobby")

    if args.check_local:
        st = user_token_status(acc, do_ping=False)
        print(json_dumps(st), flush=True)
        return 0 if st.get("age_ok_db") and st.get("has_token") else 1

    if args.ping:
        t0 = time.perf_counter()
        p = ping_user_token(acc, game_id=gid, gamename=gname)
        ms = int((time.perf_counter() - t0) * 1000)
        if p.get("ok"):
            print(f"OK ({ms}ms) code={p.get('code')}", flush=True)
            return 0
        print(
            f"FAIL ({ms}ms) {p.get('reason') or p.get('msg')} code={p.get('code')}",
            flush=True,
        )
        return 1

    if args.check:
        t0 = time.perf_counter()
        st = user_token_status(acc, do_ping=True)
        st["ping_ms"] = int((time.perf_counter() - t0) * 1000)
        print(json_dumps(st), flush=True)
        return 0 if st.get("ping_ok") else 1
    if args.import_token:
        rep = import_user_token(acc, args.import_token)
        if rep.get("ok"):
            rep["ws_token"] = fetch_ws_token(
                acc,
                game_id=int(game_by_key(args.game)["game_id"]),
                gamename=str(game_by_key(args.game).get("gamename") or "dice_md5"),
            )
            rep["ok"] = bool(rep.get("ws_token", {}).get("ok"))
        persist_session(args.account, acc)
        print(json_dumps(rep), flush=True)
        return 0 if rep.get("ok") else 1
    if args.interactive:
        g = game_by_key(args.game)
        rep = refresh_user_token_interactive(acc, game_id=int(g["game_id"]))
        if rep.get("ok"):
            rep["ws_token"] = fetch_ws_token(
                acc, game_id=int(g["game_id"]), gamename=str(g.get("gamename") or "dice_md5")
            )
            rep["ok"] = bool(rep.get("ws_token", {}).get("ok"))
        persist_session(args.account, acc)
        print(json_dumps(rep), flush=True)
        return 0 if rep.get("ok") else 1
    rep = refresh_minigame_tokens(
        acc,
        account_id=args.account,
        game_key=args.game,
        force=args.force,
        ws_only=args.ws_only,
    )
    print(json_dumps(rep), flush=True)
    return 0 if rep.get("ok") else 1


def json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    sys.exit(_main())
