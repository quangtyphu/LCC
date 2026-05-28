# -*- coding: utf-8 -*-
"""
Giữ tab vendor “active” — tránh Welcome Back / gamehallBackToGame khi đổi tab.

Game Sexy/Bikimex dùng Page Visibility + idle → đẩy về gamehallBackToGame.jsp.
"""
from __future__ import annotations

import json
import time
from typing import Any
from c168_capture_game_b import CDP_URL
from c168_vendor_bet import _cdp_request, find_vendor_tab, is_vendor_hall_redirect_url

VENDOR_ANTI_IDLE_JS = r"""
(() => {
  if (window.__c168AntiIdle) return;
  window.__c168AntiIdle = true;
  const alwaysVisible = () => {
    try {
      Object.defineProperty(document, 'visibilityState', {
        get: () => 'visible', configurable: true
      });
      Object.defineProperty(document, 'hidden', {
        get: () => false, configurable: true
      });
    } catch (e) {}
  };
  alwaysVisible();
  document.addEventListener('visibilitychange', (e) => {
    e.stopImmediatePropagation();
    alwaysVisible();
  }, true);
  window.addEventListener('blur', (e) => {
    e.stopImmediatePropagation();
    setTimeout(() => window.dispatchEvent(new Event('focus')), 0);
  }, true);
  const ping = () => {
    alwaysVisible();
    try {
      window.dispatchEvent(new Event('focus'));
      document.dispatchEvent(new Event('mousemove', { bubbles: true }));
    } catch (e) {}
  };
  setInterval(ping, 15000);
  ping();
})();
"""

JS_CLICK_BACK_TO_GAME = r"""
() => {
  const want = /back\s*to\s*the\s*game|quay\s*lại|trở\s*lại/i;
  const nodes = [...document.querySelectorAll('button, a, [role=button], div, span')];
  for (const el of nodes) {
    const t = (el.textContent || '').replace(/\s+/g, ' ').trim();
    if (!want.test(t)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 40 || r.height < 20) continue;
    try {
      el.click();
      return { ok: true, label: t.slice(0, 80) };
    } catch (e) {}
  }
  return { ok: false, reason: 'button_not_found' };
}
"""


def _vendor_sessions(cdp_base: str) -> list[tuple[str, str]]:
    """(session_wss, page_url) cho tab vendor."""
    try:
        import urllib.request

        tabs = json.loads(
            urllib.request.urlopen(f"{cdp_base.rstrip('/')}/json/list", timeout=5)
            .read()
            .decode()
        )
    except Exception:
        return []
    out: list[tuple[str, str]] = []
    for t in tabs:
        if not isinstance(t, dict):
            continue
        url = str(t.get("url") or "")
        wss = t.get("webSocketDebuggerUrl")
        if not wss:
            continue
        ul = url.lower()
        if any(k in ul for k in ("bpcdf.", "tgmeq", "mhuxu", "bikimex", "vesnamex")):
            out.append((str(wss), url))
    return out


def inject_anti_idle_all(cdp_base: str = CDP_URL) -> int:
    """Inject script chống tab hidden — gọi sau login / vào game."""
    n = 0
    for wss, _url in _vendor_sessions(cdp_base):
        try:
            _cdp_request(wss, "Runtime.enable", {}, 1)
            _cdp_request(
                wss,
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": VENDOR_ANTI_IDLE_JS},
                2,
            )
            _cdp_request(
                wss,
                "Runtime.evaluate",
                {"expression": VENDOR_ANTI_IDLE_JS, "returnByValue": True},
                3,
            )
            try:
                _cdp_request(
                    wss,
                    "Emulation.setFocusEmulationEnabled",
                    {"enabled": True},
                    4,
                )
            except Exception:
                pass
            n += 1
        except Exception:
            pass
    return n


def click_back_to_game(cdp_base: str = CDP_URL) -> dict[str, Any]:
    tab = find_vendor_tab(cdp_base, require_table=False)
    if not tab:
        return {"ok": False, "error": "no_tab"}
    wss = tab["wss"]
    _cdp_request(wss, "Runtime.enable", {}, 1)
    resp = _cdp_request(
        wss,
        "Runtime.evaluate",
        {"expression": JS_CLICK_BACK_TO_GAME, "returnByValue": True},
        2,
    )
    val = ((resp.get("result") or {}).get("result") or {}).get("value")
    if isinstance(val, dict) and val.get("ok"):
        return {"ok": True, "click": val, "url": tab.get("url")}
    return {"ok": False, "click": val, "url": tab.get("url")}


def recover_vendor_session_via_cdp(
    *,
    table_name: str,
    table_id: int,
    cdp_base: str = CDP_URL,
) -> dict[str, Any]:
    """Welcome Back → Back To Game → giả vào bàn (WS) hoặc click C06."""
    from c168_vendor_bet import try_reenter_table_via_cdp
    from c168_vendor_virtual_table import fake_enter_table_via_cdp

    tab = find_vendor_tab(cdp_base, require_table=False)
    if not tab:
        return {"ok": False, "error": "no_vendor_tab"}

    url = tab.get("url") or ""
    out: dict[str, Any] = {"from_url": url}

    if is_vendor_hall_redirect_url(url):
        back = click_back_to_game(cdp_base)
        out["back_click"] = back
        time.sleep(4 if back.get("ok") else 1)

    virtual = fake_enter_table_via_cdp(table_id, cdp_base=cdp_base)
    out["virtual_enter"] = virtual
    if virtual.get("ok"):
        inject_anti_idle_all(cdp_base)
        time.sleep(2)
        return {**out, "ok": True, "method": "ws_lobbyTableClick"}

    re = try_reenter_table_via_cdp(
        table_name=table_name, table_id=table_id, cdp_base=cdp_base
    )
    out["reenter"] = re
    if re.get("ok"):
        inject_anti_idle_all(cdp_base)
        fake_enter_table_via_cdp(table_id, cdp_base=cdp_base)
    return {**out, "ok": bool(re.get("ok")), "table_url": re.get("table_url")}
