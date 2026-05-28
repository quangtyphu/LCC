# -*- coding: utf-8 -*-
"""
Mở Chrome và vào Game B (vendor SEXY / platform 1012) dùng session profile capture.

Cách dùng (sau khi đã capture + login trên profile c168-gameb-capture):
  python c168_open_game.py
  python c168_open_game.py --platform 1012 --category 4
  python c168_open_game.py --fresh-profile   # profile mới → đăng nhập tay trước

Cơ chế: Chrome giữ profile → đọc token từ localStorage → POST gameApi/login → mở game_url.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from c168_capture_game_b import (
    CAPTURE_PORT,
    CDP_URL,
    _cdp_alive,
    _kill_capture_chrome,
    _start_chrome,
    _wipe_profile,
)

_DIR = Path(__file__).resolve().parent

HALL_API = "https://af861c.c168f.com"
GAME_LOGIN_PATH = "/hall/api/gameCenter/gameApi/login"
LOGOUT_PATH = "/hall/api/gameCenter/gameApi/logout"

_JS_SESSION_SNAPSHOT = """
() => {
  try {
    const raw = localStorage.getItem("web__lobby__persisted__token");
    if (!raw) return { ready: false, reason: "no_token" };
    const t = JSON.parse(decodeURIComponent(raw));
    const i = (t && t.tokenInfos) || {};
    const jwt = String(i.jwt_token || "");
    const finger = String(i.browserfingerid || "");
    let device = "";
    try {
      const d = localStorage.getItem("web__lobby__persisted__device");
      device = d ? JSON.parse(decodeURIComponent(d)).uuid || "" : "";
    } catch (e) {}
    const ready = !!(i.session_key && jwt.length > 40);
    return {
      ready,
      session_key: !!i.session_key,
      jwt_len: jwt.length,
      has_finger: finger.length > 8,
      device_len: device.length,
      username: String(i.username || i.account || "").trim(),
    };
  } catch (e) {
    return { ready: false, reason: String(e) };
  }
}
"""

_JS_OPEN_VENDOR = """
async ({ platformId, categoryId, gameId, maxRetries, navigate }) => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const hall = "https://af861c.c168f.com";
  const origin = location.origin || "https://c1686.net";
  const retries = maxRetries || 5;

  const readToken = () => {
    try {
      const raw = localStorage.getItem("web__lobby__persisted__token");
      if (!raw) return null;
      return JSON.parse(decodeURIComponent(raw));
    } catch (e) {
      return null;
    }
  };

  const readBrowserFinger = (infos) => {
    if (infos && infos.browserfingerid) return String(infos.browserfingerid);
    const keys = [
      "web__lobby__persisted__browserfingerid",
      "web__lobby__persisted__fingerprint",
    ];
    for (const k of keys) {
      try {
        const raw = localStorage.getItem(k);
        if (!raw) continue;
        const v = JSON.parse(decodeURIComponent(raw));
        if (typeof v === "string" && v) return v;
        if (v && (v.browserfingerid || v.fingerprint || v.id))
          return String(v.browserfingerid || v.fingerprint || v.id);
      } catch (e) {}
    }
    return "";
  };

  const buildHeaders = () => {
    const tokenBox = readToken();
    const infos = tokenBox && tokenBox.tokenInfos;
    if (!infos || !infos.session_key) return null;
    const jwt = String(infos.jwt_token || "");
    if (jwt.length < 40) return null;
    const h = {
      "accept": "application/json, text/plain, */*",
      "content-type": "application/json",
      "origin": origin,
      "referer": origin + "/",
      "token": infos.session_key,
      "newjwt": jwt,
      "sitecode": "2865",
      "domain": "c1686.net",
      "currency": "VND",
      "device": (() => {
        try {
          const d = localStorage.getItem("web__lobby__persisted__device");
          return d ? JSON.parse(decodeURIComponent(d)).uuid || "" : "";
        } catch (e) { return ""; }
      })(),
      "x-data-mode": "plain",
      "x-version": "7.2.162",
      "appversion": "v7.2.162",
      "x-device": "1-1",
      "timestamp": String(Math.floor(Date.now() / 1000)),
    };
    const finger = readBrowserFinger(infos);
    if (finger) h["browserfingerid"] = finger;
    return h;
  };

  let lastFail = null;
  for (let attempt = 0; attempt < retries; attempt++) {
    const headers = buildHeaders();
    if (!headers) {
      return { ok: false, error: "chua_dang_nhap", hint: "Thiếu session_key/newjwt" };
    }

    const now = Math.floor(Date.now() / 1000);
    const bodyLogin = {
      os_type: 3,
      gameid: gameId,
      platfromid: platformId,
      exitUrl: "",
      cid: "999998",
      time: now,
    };

    try {
      await fetch(hall + "/hall/api/gameCenter/gameApi/logout", {
        method: "POST",
        headers,
        body: JSON.stringify({ os_type: 3, callContext: "c168_open_game", time: now }),
      });
    } catch (e) {}

    await sleep(400 + attempt * 400);

    const resp = await fetch(hall + "/hall/api/gameCenter/gameApi/login", {
      method: "POST",
      headers,
      body: JSON.stringify(bodyLogin),
    });
    const text = await resp.text();
    let data = null;
    try { data = JSON.parse(text); } catch (e) {}
    if (resp.ok && data && data.code === 1) {
      const gameUrl =
        (data.data && (data.data.game_url || (data.data.url && data.data.url[0] && data.data.url[0].url))) ||
        "";
      if (!gameUrl) {
        return { ok: false, error: "no_game_url", body: text.slice(0, 500) };
      }
      if (navigate !== false) {
        window.location.href = gameUrl;
      }
      return {
        ok: true,
        game_url: gameUrl,
        platfromid: data.data.platfromid,
        gameid: data.data.gameid,
        attempt,
        navigated: navigate !== false,
      };
    }
    lastFail = {
      ok: false,
      error: "gameApi_login_fail",
      status: resp.status,
      body: text.slice(0, 500),
      code: data && data.code,
      attempt,
    };
    const code = data && (data.code || data.err_code);
    if (code === 1401 || code === 41001401) {
      await sleep(1200 + attempt * 800);
      continue;
    }
    return lastFail;
  }
  return lastFail || { ok: false, error: "gameApi_login_fail" };
}
"""


def _session_snapshot(page) -> dict[str, Any]:
    try:
        out = page.evaluate(_JS_SESSION_SNAPSHOT)
        return out if isinstance(out, dict) else {"ready": False}
    except Exception as e:
        return {"ready": False, "reason": str(e)}


def wait_hall_session(page, *, timeout_sec: int = 90) -> dict[str, Any]:
    """Chờ localStorage có session_key + JWT (sau member/login)."""
    deadline = time.time() + timeout_sec
    last: dict[str, Any] = {"ready": False}
    while time.time() < deadline:
        last = _session_snapshot(page)
        if last.get("ready"):
            return last
        page.wait_for_timeout(500)
    return last


def settle_after_login(page, *, site: str = "https://c1686.net") -> dict[str, Any]:
    """Chờ JWT; chỉ về trang chủ nếu session chưa sẵn (tránh reload thừa)."""
    snap = wait_hall_session(page, timeout_sec=15)
    if snap.get("ready"):
        return snap

    home = site.rstrip("/") + "/"
    url = (page.url or "").lower()
    on_home = url.rstrip("/") in (home.rstrip("/"), site.rstrip("/").lower())
    if not on_home and "/home/login" in url:
        print("Chờ session sau login — về trang chủ (1 lần)…", file=sys.stderr)
        try:
            page.goto(home, wait_until="domcontentloaded", timeout=120_000)
        except Exception:
            pass
        page.wait_for_timeout(2000)
    else:
        page.wait_for_timeout(1500)

    snap = wait_hall_session(page, timeout_sec=60)
    page.wait_for_timeout(800)
    return snap


def _lobby_url(category_id: int, platform_id: int) -> str:
    return f"https://c1686.net/home/subgame?gameCategoryId={category_id}&platformId={platform_id}"


def fetch_vendor_game_url(
    page,
    *,
    platform_id: int,
    category_id: int,
    game_id: int,
    max_retries: int = 5,
) -> dict[str, Any]:
    """gameApi/login — chỉ lấy URL, không chuyển tab hiện tại (mở vendor tab nền)."""
    snap = wait_hall_session(page, timeout_sec=30)
    if not snap.get("ready"):
        return {"ok": False, "error": "session_not_ready", "session": snap}
    payload = {
        "platformId": platform_id,
        "categoryId": category_id,
        "gameId": game_id,
        "maxRetries": max_retries,
        "navigate": False,
    }
    out = page.evaluate(_JS_OPEN_VENDOR, payload)
    if isinstance(out, dict) and out.get("ok"):
        return {
            "ok": True,
            "game_url": out.get("game_url"),
            "platform_id": platform_id,
            "game_id": game_id,
        }
    lobby = _lobby_url(category_id, platform_id)
    try:
        page.goto(lobby, wait_until="domcontentloaded", timeout=120_000)
    except Exception:
        pass
    page.wait_for_timeout(2500)
    out = page.evaluate(_JS_OPEN_VENDOR, payload)
    if isinstance(out, dict) and out.get("ok"):
        return {
            "ok": True,
            "game_url": out.get("game_url"),
            "platform_id": platform_id,
            "game_id": game_id,
        }
    return {
        "ok": False,
        "error": out.get("error") if isinstance(out, dict) else "evaluate_failed",
        "detail": out,
    }


def open_vendor_game(
    *,
    platform_id: int = 1012,
    category_id: int = 4,
    game_id: int | None = None,
    fresh_profile: bool = False,
    keep_chrome: bool = True,
) -> dict[str, Any]:
    if game_id is None:
        game_id = platform_id * 10_000  # 10120000 = lobby SEXY

    result: dict[str, Any] = {
        "ok": False,
        "platform_id": platform_id,
        "category_id": category_id,
        "game_id": game_id,
    }

    if fresh_profile:
        print("Profile mới (xóa c168-gameb-capture)…", file=sys.stderr)
        _wipe_profile()
    elif not _cdp_alive():
        _kill_capture_chrome()
        time.sleep(0.5)

    lobby = _lobby_url(category_id, platform_id)
    if not _cdp_alive():
        print(f"Mở Chrome port {CAPTURE_PORT}…", file=sys.stderr)
        ok, msg = _start_chrome("https://c1686.net/", proxy="")
        if not ok:
            result["error"] = msg
            return result
        print(f"Chrome: {msg}", file=sys.stderr)
    else:
        print(f"Chrome capture đang chạy ({CDP_URL})", file=sys.stderr)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        result["error"] = "pip install playwright && playwright install chromium"
        return result

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            result["error"] = f"CDP: {e}"
            return result

        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()

        print(f"Mở lobby vendor: {lobby}", file=sys.stderr)
        try:
            page.goto(lobby, wait_until="domcontentloaded", timeout=120_000)
        except Exception as e:
            print(f"Goto lobby: {e}", file=sys.stderr)

        page.wait_for_timeout(2500)

        print(
            f"Gọi gameApi/login (platfromid={platform_id}, gameid={game_id})…",
            file=sys.stderr,
        )
        out = page.evaluate(
            _JS_OPEN_VENDOR,
            {
                "platformId": platform_id,
                "categoryId": category_id,
                "gameId": game_id,
            },
        )

        if not isinstance(out, dict) or not out.get("ok"):
            result["error"] = out.get("error") if isinstance(out, dict) else "evaluate_failed"
            result["detail"] = out
            if result.get("error") == "chua_dang_nhap":
                print(
                    "\n→ Chưa login trong profile này. Chạy:\n"
                    "  python c168_capture_game_b.py\n"
                    "  (đăng nhập tay, vào SEXY một lần, tắt Chrome)\n"
                    "  rồi: python c168_open_game.py\n",
                    file=sys.stderr,
                )
            else:
                print(f"Thất bại: {out!r}", file=sys.stderr)
            if not keep_chrome:
                browser.close()
            return result

        game_url = str(out.get("game_url") or "")
        result["ok"] = True
        result["game_url"] = game_url
        print(f"\nOK — chuyển sang vendor:\n  {game_url[:120]}…\n", file=sys.stderr)

        page.wait_for_timeout(5000)

        if keep_chrome:
            print("Giữ Chrome mở — chơi / xem game trên tab vendor.", file=sys.stderr)
        else:
            browser.close()

    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Mở Chrome vào Game B (SEXY / platform 1012)")
    ap.add_argument("--platform", type=int, default=1012, help="platfromid (SEXY=1012)")
    ap.add_argument("--category", type=int, default=4, help="gameCategoryId live")
    ap.add_argument("--game-id", type=int, default=0, help="gameid (0 = lobby = platform*10000)")
    ap.add_argument("--fresh-profile", action="store_true", help="Xóa profile capture (login lại)")
    ap.add_argument("--close", action="store_true", help="Đóng browser sau khi mở")
    args = ap.parse_args()

    gid = args.game_id if args.game_id > 0 else None

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    out = open_vendor_game(
        platform_id=args.platform,
        category_id=args.category,
        game_id=gid,
        fresh_profile=args.fresh_profile,
        keep_chrome=not args.close,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
