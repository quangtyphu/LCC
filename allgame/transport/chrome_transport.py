# -*- coding: utf-8 -*-
"""
Chrome transport — skeleton.

Luồng đầy đủ (triển khai sau):
  portal.token.test_token → vendor.open_game → vendor.enter_table
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
import zlib
from pathlib import Path
from typing import Any

from allgame.db.accounts_db import (
    account_session_key,
    get_account,
    resolve_profile_dir,
    update_session,
    upsert_account,
)
from allgame.db.constants import TRANSPORT_CHROME, VENDOR_IDLE
from allgame.orchestrator.session_registry import ActiveSession, SessionRegistry
from allgame.portals.registry import get_portal_bundle
from allgame.vendor.ws_connector import connect_vendor_ws


class ChromeTransport:
    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        self.cfg = cfg or {}

    def connect(
        self,
        account: dict[str, Any],
        *,
        registry: SessionRegistry,
    ) -> dict[str, Any]:
        portal_id = str(account.get("portal_id") or "").strip().lower()
        username = str(account.get("username") or "").strip()
        key = account_session_key(portal_id, username)

        bundle = get_portal_bundle(portal_id)
        if not bundle:
            return {"ok": False, "error": "unknown_portal", "session_key": key}

        chrome = self._ensure_chrome_for_account(account)
        if not chrome.get("ok"):
            return {
                "ok": False,
                "error": "open_chrome_failed",
                "session_key": key,
                "detail": chrome,
            }

        if portal_id == "c168":
            try:
                from allgame.portals.c168.open_chrome_token import read_and_update_token
                from allgame.portals.c168.check_balance import check_balance
            except Exception as e:
                return {
                    "ok": False,
                    "error": f"import_c168_check_balance_failed: {e}",
                    "session_key": key,
                }
            print(f"[ALLGAME][C168] {username} | token + balance…", flush=True)
            t0 = time.time()
            token_out = read_and_update_token(username, chrome=chrome)
            if not token_out.get("ok"):
                return {
                    "ok": False,
                    "error": "c168_open_chrome_or_token_failed",
                    "session_key": key,
                    "detail": token_out,
                }
            snap = token_out.get("token_snapshot") if isinstance(token_out.get("token_snapshot"), dict) else {}
            bal_val = token_out.get("balance_value")
            if bal_val is not None and snap.get("ok"):
                bal = {
                    "ok": True,
                    "balance": bal_val,
                    "status": snap.get("status"),
                    "code": snap.get("code"),
                    "source": "token_snapshot",
                }
            else:
                bal = check_balance(username)
            if not bal.get("ok"):
                return {
                    "ok": False,
                    "error": "c168_balance_check_failed",
                    "session_key": key,
                    "detail": bal,
                }
            print(
                f"[ALLGAME][C168] {username} | token OK ({time.time() - t0:.1f}s) → Game B…",
                flush=True,
            )
            ws = connect_vendor_ws(account, chrome=chrome, cfg=self.cfg)
            if not ws.get("ok"):
                return {
                    "ok": False,
                    "error": "vendor_ws_connect_failed",
                    "session_key": key,
                    "detail": ws,
                }
            session = ActiveSession(
                session_key=key,
                portal_id=portal_id,
                username=username,
                transport=TRANSPORT_CHROME,
                state="ready_to_bet",
                chrome_cdp_url=str(chrome.get("cdp_url") or ""),
                meta={
                    "balance": bal.get("balance"),
                    "status": bal.get("status"),
                    "code": bal.get("code"),
                    "source": bal.get("source"),
                    "chrome": chrome,
                    "token_snapshot": token_out.get("token_snapshot"),
                    "ready_to_bet": bool(ws.get("ready_to_bet")),
                    "ws_connected": bool(ws.get("ws_connected")),
                    "ws_urls": ws.get("ws_urls"),
                },
            )
            registry.upsert(session)
            return {
                "ok": True,
                "session_key": key,
                "balance": bal.get("balance"),
                "token_alive": bool(token_out.get("ok")),
                "status": bal.get("status"),
                "code": bal.get("code"),
                "ready_to_bet": bool(ws.get("ready_to_bet")),
                "enter_table_ok": ws.get("enter_table_ok"),
                "enter_table_method": ws.get("enter_table_method"),
            }

        token_out = self._read_and_update_token_from_chrome(
            portal_id=portal_id,
            username=username,
            cdp_url=str(chrome.get("cdp_url") or ""),
        )
        if not token_out.get("ok"):
            return {
                "ok": False,
                "error": "open_chrome_or_token_failed",
                "session_key": key,
                "detail": token_out,
            }
        acc2 = get_account(portal_id, username) or account
        snap = bundle.token.read_token_snapshot(acc2)
        if not snap.get("ok"):
            return {
                "ok": False,
                "error": "token_check_failed",
                "session_key": key,
                "detail": snap,
            }
        bal_val = self._to_float_balance(snap.get("balance"))
        if bal_val is not None:
            upsert_account({"portal_id": portal_id, "username": username, "balance": bal_val})
        ws = connect_vendor_ws(acc2, chrome=chrome, cfg=self.cfg)
        if not ws.get("ok"):
            return {
                "ok": False,
                "error": "vendor_ws_connect_failed",
                "session_key": key,
                "detail": ws,
            }
        session = ActiveSession(
            session_key=key,
            portal_id=portal_id,
            username=username,
            transport=TRANSPORT_CHROME,
            state="ready_to_bet",
            chrome_cdp_url=str(chrome.get("cdp_url") or ""),
            meta={
                "note": "ready_to_bet",
                "chrome": chrome,
                "token_snapshot": snap,
                "balance": bal_val,
                "ready_to_bet": bool(ws.get("ready_to_bet")),
                "ws_connected": bool(ws.get("ws_connected")),
                "ws_urls": ws.get("ws_urls"),
            },
        )
        registry.upsert(session)
        return {
            "ok": True,
            "session_key": key,
            "launched": chrome.get("launched"),
            "token_alive": bool(snap.get("ok")),
            "balance": bal_val,
            "status": snap.get("status"),
            "code": snap.get("code"),
            "ready_to_bet": bool(ws.get("ready_to_bet")),
            "enter_table_ok": ws.get("enter_table_ok"),
            "enter_table_method": ws.get("enter_table_method"),
        }

    def disconnect(
        self,
        session_key: str,
        *,
        registry: SessionRegistry,
    ) -> dict[str, Any]:
        removed = registry.remove(session_key)
        closed_detail = None
        if removed is not None and str(removed.portal_id or "").lower() == "c168":
            try:
                from allgame.vendor.c168_vendor_flow import release_listen_sniffer

                release_listen_sniffer(str(removed.chrome_cdp_url or ""))
            except Exception:
                pass
        if removed is not None:
            acc = get_account(str(removed.portal_id or ""), str(removed.username or ""))
            closed_detail = self._close_chrome_for_account(
                portal_id=str(removed.portal_id or ""),
                username=str(removed.username or ""),
                account=acc,
            )
        return {
            "ok": True,
            "session_key": session_key,
            "was_active": removed is not None,
            "closed_chrome": closed_detail,
        }

    def health_check(self, session_key: str, *, registry: SessionRegistry) -> bool:
        return registry.get(session_key) is not None

    def _default_cdp_port(self, portal_id: str, username: str) -> int:
        key = f"{portal_id}:{username}".strip().lower().encode("utf-8")
        if not key:
            return 10600
        h = zlib.adler32(key) & 0x7FFF
        return 9361 + (h % 1200)

    def _cdp_alive(self, cdp_url: str) -> bool:
        try:
            with urllib.request.urlopen(f"{cdp_url.rstrip('/')}/json/version", timeout=2) as resp:
                return int(resp.status or 0) == 200
        except Exception:
            return False

    def _ensure_chrome_for_account(self, account: dict[str, Any]) -> dict[str, Any]:
        portal_id = str(account.get("portal_id") or "").strip().lower()
        username = str(account.get("username") or "").strip()
        profile_dir = resolve_profile_dir(
            portal_id=portal_id,
            username=username,
            stored_dir=str(account.get("chrome_browser_dir") or ""),
        )
        try:
            cdp_port = int(account.get("chrome_cdp_port") or 0)
        except (TypeError, ValueError):
            cdp_port = 0
        if cdp_port < 1:
            cdp_port = self._default_cdp_port(portal_id, username)
        cdp_url = f"http://127.0.0.1:{cdp_port}"
        if self._cdp_alive(cdp_url):
            return {"ok": True, "launched": False, "cdp_url": cdp_url, "profile_dir": profile_dir}

        proxy = str(account.get("proxy") or "").strip()
        launcher = Path(__file__).resolve().parent.parent.parent / "browser_isolate" / "cms_launch.py"
        if not launcher.is_file():
            return {"ok": False, "error": f"missing_launcher:{launcher}"}
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
        python = os.environ.get("PYTHON") or os.sys.executable
        url = self._portal_home_url(portal_id)
        args = [
            str(launcher),
            "--profile-dir",
            str(profile_dir),
            "--proxy",
            proxy,
            "--cdp-port",
            str(cdp_port),
            "--urls-json",
            json.dumps([url]),
        ]
        proc = subprocess.Popen(
            [python, *args],
            cwd=str(launcher.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(36):
            if self._cdp_alive(cdp_url):
                return {
                    "ok": True,
                    "launched": True,
                    "cdp_url": cdp_url,
                    "profile_dir": profile_dir,
                    "launcher_pid": proc.pid,
                }
            if proc.poll() is not None and proc.returncode not in (0, None):
                return {
                    "ok": False,
                    "error": f"launcher_exit_{proc.returncode}",
                }
            time.sleep(0.25)
        return {"ok": False, "error": f"cdp_not_ready:{cdp_url}"}

    def _close_chrome_for_account(
        self,
        *,
        portal_id: str,
        username: str,
        account: dict[str, Any] | None,
    ) -> dict[str, Any]:
        acc = account or {}
        try:
            cdp_port = int(acc.get("chrome_cdp_port") or 0)
        except (TypeError, ValueError):
            cdp_port = 0
        if cdp_port < 1:
            cdp_port = self._default_cdp_port(portal_id, username)
        cdp_url = f"http://127.0.0.1:{cdp_port}"
        if not self._cdp_alive(cdp_url):
            return {"ok": True, "closed": False, "reason": "already_closed", "cdp_url": cdp_url}
        pids: list[int] = []
        proc = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            timeout=20,
            encoding="utf-8",
            errors="replace",
        )
        needle = f":{cdp_port}"
        for line in (proc.stdout or "").splitlines():
            row = " ".join(line.split())
            if needle not in row or "LISTENING" not in row.upper():
                continue
            parts = row.split(" ")
            if len(parts) < 5:
                continue
            try:
                pid = int(parts[-1])
            except ValueError:
                continue
            if pid > 0 and pid not in pids:
                pids.append(pid)
        for pid in pids:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    encoding="utf-8",
                    errors="replace",
                )
            except Exception:
                continue
        time.sleep(0.5)
        return {"ok": not self._cdp_alive(cdp_url), "closed": bool(pids), "pids": pids, "cdp_url": cdp_url}

    def _portal_home_url(self, portal_id: str) -> str:
        portal = str(portal_id or "").strip().lower()
        defaults = {
            "c168": "https://c168b2.cc/",
            "f168": "https://f1686s.com/",
            "fly88": "https://m.fly88t.vip/",
        }
        from_cfg = self.cfg.get("portal_home_urls")
        if isinstance(from_cfg, dict):
            raw = from_cfg.get(portal)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        return defaults.get(portal, "about:blank")

    def _portal_hall_origin(self, portal_id: str) -> str:
        p = str(portal_id or "").strip().lower()
        defaults = {
            "c168": "https://af861c.c168f.com",
            "f168": "https://ah861f.f1sau8.com",
            "fly88": "https://ok.fly88b.cc",
        }
        from_cfg = self.cfg.get("portal_hall_origins")
        if isinstance(from_cfg, dict):
            raw = from_cfg.get(p)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        return defaults.get(p, "")

    def _read_and_update_token_from_chrome(
        self,
        *,
        portal_id: str,
        username: str,
        cdp_url: str,
    ) -> dict[str, Any]:
        if not cdp_url:
            return {"ok": False, "error": "missing_cdp_url"}
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            return {
                "ok": False,
                "error": "playwright_not_installed",
                "hint": "pip install playwright && playwright install chromium",
            }
        home_url = self._portal_home_url(portal_id)
        hall = self._portal_hall_origin(portal_id)
        js = """
() => {
  try {
    const raw = localStorage.getItem("web__lobby__persisted__token");
    if (!raw) return { ok: false, error: "no_token_in_local_storage" };
    const tok = JSON.parse(decodeURIComponent(raw));
    const info = (tok && tok.tokenInfos) || {};
    const sessionKey = String(info.session_key || "");
    const jwt = String(info.jwt_token || "");
    let device = "";
    try {
      const dRaw = localStorage.getItem("web__lobby__persisted__device");
      device = dRaw ? String((JSON.parse(decodeURIComponent(dRaw)) || {}).uuid || "") : "";
    } catch (e) {}
    const out = {
      ok: !!(sessionKey && jwt.length > 40),
      session_key: sessionKey,
      newjwt: jwt,
      device: device,
      browserfingerid: String(info.browserfingerid || ""),
      domain: location.host || "",
      origin: location.origin || "",
      appversion: "v7.3.17",
      "x-version": "7.3.17",
      sitecode: String(info.sitecode || ""),
      x_device: "1-1"
    };
    if (!out.ok) out.error = "missing_session_key_or_jwt";
    return out;
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}
"""
        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp(cdp_url)
            except Exception as e:
                return {"ok": False, "error": f"connect_cdp_failed:{e}"}
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            if home_url and home_url.startswith("http") and home_url not in str(page.url or ""):
                try:
                    page.goto(home_url, wait_until="domcontentloaded", timeout=120_000)
                except Exception:
                    pass
                page.wait_for_timeout(1200)
            snap = page.evaluate(js)
        if not isinstance(snap, dict) or not snap.get("ok"):
            return {"ok": False, "error": str((snap or {}).get("error") if isinstance(snap, dict) else "bad_js_result")}
        patch = {k: snap.get(k) for k in ("session_key", "newjwt", "device", "browserfingerid", "domain", "origin", "appversion", "x-version", "sitecode", "x_device") if snap.get(k)}
        if hall:
            patch["hall_api"] = hall
        update_session(portal_id, username, patch)
        return {"ok": True, "session_patch": patch}

    @staticmethod
    def _to_float_balance(raw: Any) -> float | None:
        if raw is None:
            return None
        try:
            return float(str(raw).replace(",", "").strip())
        except (TypeError, ValueError):
            return None
