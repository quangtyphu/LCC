#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mở Chrome (cms_launch) + đọc token lobby → JSON một dòng (CMS lưu session_json).

  python allgame_open_chrome.py --portal c168 -u USER \\
    --profile-dir "..." --cdp-port 9516 --proxy host:port:user:pass \\
    --site-url https://c168b2.cc/ --cdp-wait 6
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parent
_REPO = _DIR.parent
_BROWSER_ISOLATE = _REPO / "browser_isolate"
_CMS_LAUNCH = _BROWSER_ISOLATE / "cms_launch.py"
_C168_DIR = _REPO / "c168_standalone"

if str(_BROWSER_ISOLATE) not in sys.path:
    sys.path.insert(0, str(_BROWSER_ISOLATE))


def _emit(obj: dict[str, Any]) -> None:
    """UTF-8 stdout — tranh loi cp1252 tren Windows khi CMS goi script."""
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    try:
        sys.stdout.buffer.write(line.encode("utf-8"))
        sys.stdout.buffer.flush()
    except Exception:
        print(json.dumps(obj, ensure_ascii=True), flush=True)


def _cdp_alive(url: str) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/json/version", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _launch_chrome(
    *,
    profile_dir: str,
    cdp_port: int,
    proxy: str,
    site_url: str,
) -> dict[str, Any]:
    if not _CMS_LAUNCH.is_file():
        return {"ok": False, "error": f"Không thấy {_CMS_LAUNCH}"}
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    python = os.environ.get("PYTHON") or sys.executable
    args = [
        str(_CMS_LAUNCH),
        "--profile-dir",
        profile_dir,
        "--proxy",
        proxy,
        "--cdp-port",
        str(cdp_port),
        "--urls-json",
        json.dumps([site_url or "about:blank"]),
    ]
    try:
        proc = subprocess.run(
            [python, *args],
            cwd=str(_BROWSER_ISOLATE),
            capture_output=True,
            text=True,
            timeout=90,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as e:
        return {"ok": False, "error": str(e)}
    line = (proc.stdout or "").strip().split("\n")[-1] if proc.stdout else ""
    try:
        data = json.loads(line or "{}")
    except json.JSONDecodeError:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        return {"ok": False, "error": err}
    if not data.get("ok"):
        return {"ok": False, "error": str(data.get("error") or "cms_launch thất bại")}
    cdp_url = f"http://127.0.0.1:{cdp_port}"
    return {
        "ok": True,
        "chrome_browser_dir": profile_dir,
        "cdp_port": cdp_port,
        "cdp_url": cdp_url,
        "pid": data.get("pid"),
        "proxy_label": data.get("proxy_label"),
    }


def _c168_session_snapshot(page) -> dict[str, Any]:
    if str(_C168_DIR) not in sys.path:
        sys.path.insert(0, str(_C168_DIR))
    from c168_open_game import _JS_SESSION_SNAPSHOT

    try:
        snap = page.evaluate(_JS_SESSION_SNAPSHOT)
        return snap if isinstance(snap, dict) else {}
    except Exception as e:
        return {"ready": False, "reason": str(e)}


def _generic_token_snapshot(page) -> dict[str, Any]:
    js = """
    () => {
      const keys = [];
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (/token|jwt|session|auth|lobby/i.test(k || "")) keys.push(k);
      }
      const out = { keys, items: {} };
      for (const k of keys.slice(0, 20)) {
        try {
          const raw = localStorage.getItem(k);
          out.items[k] = raw && raw.length < 8000 ? raw : (raw ? raw.slice(0, 200) + "…" : "");
        } catch (e) {}
      }
      return out;
    }
    """
    try:
        return page.evaluate(js) or {}
    except Exception as e:
        return {"error": str(e)}


def _capture_session(
    *,
    portal_id: str,
    cdp_url: str,
    site_url: str,
    cdp_wait: float,
) -> dict[str, Any]:
    if cdp_wait > 0:
        time.sleep(cdp_wait)
    if not _cdp_alive(cdp_url):
        return {
            "ok": False,
            "error": f"CDP chưa sẵn sàng ({cdp_url})",
            "session_alive": False,
        }
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "error": "pip install playwright"}

    portal = str(portal_id or "").strip().lower()
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            return {"ok": False, "error": f"CDP: {e}", "session_alive": False}

        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
        try:
            if site_url and "about:" not in site_url:
                page.goto(site_url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2000)
        except Exception:
            pass

        if portal == "c168":
            snap = _c168_session_snapshot(page)
            alive = bool(snap.get("ready"))
            session_json = {
                "portal_id": portal,
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "lobby_snapshot": snap,
            }
            if alive:
                try:
                    raw = page.evaluate(
                        """() => {
                          try {
                            return localStorage.getItem("web__lobby__persisted__token");
                          } catch (e) { return ""; }
                        }"""
                    )
                    if raw:
                        session_json["web__lobby__persisted__token"] = raw
                except Exception:
                    pass
        else:
            snap = _generic_token_snapshot(page)
            alive = bool(snap.get("keys"))
            session_json = {
                "portal_id": portal,
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "lobby_snapshot": snap,
            }

        return {
            "ok": True,
            "session_alive": alive,
            "session": snap,
            "session_json": session_json,
        }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--portal", required=True)
    ap.add_argument("-u", "--username", required=True)
    ap.add_argument("--profile-dir", required=True)
    ap.add_argument("--cdp-port", type=int, required=True)
    ap.add_argument("--proxy", required=True)
    ap.add_argument("--site-url", default="about:blank")
    ap.add_argument("--cdp-wait", type=float, default=6.0)
    ap.add_argument(
        "--skip-launch",
        action="store_true",
        help="Chỉ capture token (Chrome đã mở)",
    )
    args = ap.parse_args()

    cdp_url = f"http://127.0.0.1:{int(args.cdp_port)}"
    out: dict[str, Any] = {
        "ok": True,
        "portal_id": args.portal,
        "username": args.username,
        "cdp_url": cdp_url,
    }

    if not args.skip_launch and not _cdp_alive(cdp_url):
        launch = _launch_chrome(
            profile_dir=args.profile_dir,
            cdp_port=int(args.cdp_port),
            proxy=args.proxy.strip(),
            site_url=args.site_url.strip() or "about:blank",
        )
        out["launch"] = launch
        if not launch.get("ok"):
            out["ok"] = False
            out["error"] = launch.get("error")
            _emit(out)
            return 1
        for _ in range(40):
            if _cdp_alive(cdp_url):
                break
            time.sleep(0.35)
    elif args.skip_launch and not _cdp_alive(cdp_url):
        out["ok"] = False
        out["error"] = f"Chrome chưa chạy ({cdp_url})"
        _emit(out)
        return 1

    cap = _capture_session(
        portal_id=args.portal,
        cdp_url=cdp_url,
        site_url=args.site_url.strip() or "about:blank",
        cdp_wait=float(args.cdp_wait or 0),
    )
    out.update(cap)
    out["chrome_browser_dir"] = args.profile_dir
    out["cdp_port"] = int(args.cdp_port)
    cdp_up = _cdp_alive(cdp_url)
    if cap.get("session_alive"):
        out["message"] = "Chrome OK - da doc token lobby"
    elif cdp_up:
        out["ok"] = True
        out["message"] = (
            cap.get("error")
            or "Chrome da mo - chua co token (dang nhap tay tren web roi mo lai)"
        )
    else:
        out["ok"] = False
        out["message"] = cap.get("error") or f"CDP khong san sang ({cdp_url})"
    _emit(out)
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        _emit({"ok": False, "error": str(e)})
        raise SystemExit(1) from e
