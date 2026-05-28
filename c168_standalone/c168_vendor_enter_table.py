# -*- coding: utf-8 -*-
"""
Tự vào bàn baccarat trên vendor — **click sảnh** (không mở thẳng singleBacTable.jsp).

Mở thẳng URL bàn làm client lỗi (màn đen, Dealer: --). Phải vào sảnh → bấm C06 như tay.

  python c168_vendor_enter_table.py
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from typing import Any
from urllib.parse import urlparse

from c168_capture_game_b import CDP_URL

VENDOR_HOST_MARKERS = (
    "bpcdf.",
    "tgmeq",
    "vesnamex",
    "mhuxu",
    "bikimex",
    "intplaynet.com/player",
)

DEFAULT_TABLE_NAME = "C06"
DEFAULT_TABLE_ID = 1006

_JS_IS_LOBBY = """
() => {
  const u = (location.href || '').toLowerCase();
  if (u.includes('singlebactable')) return false;
  const body = (document.body && document.body.innerText) || '';
  return (
    body.includes('Baccarat') ||
    /\\bC0[0-9]\\b/.test(body) ||
    body.includes('Tables') ||
    body.includes('Lobby')
  );
}
"""

_JS_CLICK_LOBBY_TABLE = """
({ tableName, tableId }) => {
  const u = (location.href || '').toLowerCase();
  if (u.includes('singlebactable')) {
    return { ok: false, reason: 'on_table_page' };
  }
  const name = String(tableName || '').trim();
  const codeRe = new RegExp('(?:Baccarat\\\\s+)?' + name + '(?:\\\\s|$|\\\\n)', 'i');

  const scoreEl = (el, doc) => {
    const t = (el.textContent || '').replace(/\\s+/g, ' ').trim();
    if (!codeRe.test(t)) return null;
    const r = el.getBoundingClientRect();
    if (r.width < 36 || r.height < 28 || r.bottom < 0 || r.right < 0) return null;
    const area = r.width * r.height;
    if (area > 120000) return null;
    let clickEl = el;
    for (let p = el; p && p !== doc.body; p = p.parentElement) {
      const tag = (p.tagName || '').toUpperCase();
      if (
        tag === 'A' ||
        tag === 'BUTTON' ||
        p.getAttribute('role') === 'button' ||
        typeof p.onclick === 'function' ||
        /table|card|item|room|bac/i.test(String(p.className || ''))
      ) {
        clickEl = p;
        break;
      }
    }
    return { clickEl, area, label: t.slice(0, 100) };
  };

  const hit = (doc) => {
    if (!doc) return null;
    const byAttr = doc.querySelector(
      `[data-table-id="${tableId}"],[data-tableid="${tableId}"],[data-id="${tableId}"]`
    );
    if (byAttr) {
      const r = byAttr.getBoundingClientRect();
      if (r.width > 20 && r.height > 12) {
        byAttr.click();
        return { ok: true, how: 'attr', label: name };
      }
    }
    const picks = [];
    for (const el of doc.querySelectorAll('div,li,a,span,td,section,article')) {
      const s = scoreEl(el, doc);
      if (s) picks.push(s);
    }
    picks.sort((a, b) => a.area - b.area);
    for (const p of picks) {
      try {
        p.clickEl.dispatchEvent(
          new MouseEvent('click', { bubbles: true, cancelable: true, view: window })
        );
      } catch (e) {
        p.clickEl.click();
      }
      return { ok: true, how: 'lobby_card', label: p.label, area: p.area };
    }
    return null;
  };

  let r = hit(document);
  if (r) return r;
  for (const fr of document.querySelectorAll('iframe')) {
    try {
      r = hit(fr.contentDocument);
      if (r) return { ...r, iframe: true };
    } catch (e) {}
  }
  return { ok: false, reason: 'not_found' };
}
"""

_JS_TABLE_VISIBLE = """
({ tableName, tableId }) => {
  const u = (location.href || '').toLowerCase();
  if (u.includes('singlebactable')) return { visible: false, onTable: true };
  const name = String(tableName || '').trim();
  const codeRe = new RegExp('(?:Baccarat\\\\s+)?' + name + '(?:\\\\s|$|\\\\n)', 'i');
  const check = (doc) => {
    if (!doc) return false;
    const byAttr = doc.querySelector(
      `[data-table-id="${tableId}"],[data-tableid="${tableId}"],[data-id="${tableId}"]`
    );
    if (byAttr) {
      const r = byAttr.getBoundingClientRect();
      if (r.width > 20 && r.height > 12 && r.bottom > 0 && r.right > 0) return true;
    }
    for (const el of doc.querySelectorAll('div,li,a,span,td,section,article')) {
      const t = (el.textContent || '').replace(/\\s+/g, ' ').trim();
      if (!codeRe.test(t)) continue;
      const r = el.getBoundingClientRect();
      if (r.width >= 36 && r.height >= 28 && r.bottom > 0 && r.right > 0) {
        const area = r.width * r.height;
        if (area > 0 && area <= 120000) return true;
      }
    }
    return false;
  };
  if (check(document)) return { visible: true };
  for (const fr of document.querySelectorAll('iframe')) {
    try {
      if (check(fr.contentDocument)) return { visible: true, iframe: true };
    } catch (e) {}
  }
  return { visible: false };
}
"""

_JS_TABLE_HEALTH = """
({ tableName, tableId }) => {
  const body = (document.body && document.body.innerText) || '';
  const u = location.href.toLowerCase();
  const name = String(tableName || 'C06');
  const tid = Number(tableId) || 1006;
  const onTable = u.includes('singlebactable');
  const broken =
    onTable &&
    (body.includes('Dealer: --') || body.includes('Dealer:--')) &&
    !body.includes('Baccarat ' + name) &&
    !body.includes('PLAYER');
  const goodOnTable =
    onTable &&
    (body.includes('Baccarat ' + name) ||
      (body.includes(name) && (body.includes('Confirm') || body.includes('PLAYER'))));
  const hasChips = /\\b10\\b/.test(body) && /\\b20\\b/.test(body);
  const urlOk = onTable && (u.includes('tableid=' + tid) || u.includes('tableid=' + tid));
  return {
    healthy: Boolean(goodOnTable && !broken && (hasChips || body.includes('PLAYER'))),
    broken,
    goodOnTable,
    hasChips,
    onTable,
    urlOk,
    url: location.href.slice(0, 120)
  };
}
"""


def _url_is_vendor(url: str) -> bool:
    u = (url or "").lower()
    return any(m in u for m in VENDOR_HOST_MARKERS)


def _is_table_url(url: str) -> bool:
    return "singlebactable" in (url or "").lower()


def _is_lobby_url(url: str) -> bool:
    u = (url or "").lower()
    return _url_is_vendor(u) and not _is_table_url(u)


def _vendor_origin(url: str) -> str:
    p = urlparse(url or "")
    if p.scheme and p.netloc:
        return f"{p.scheme}://{p.netloc}"
    return ""


def find_vendor_page(
    context: Any, *, timeout_sec: float = 90, prefer_lobby: bool = True
) -> Any | None:
    deadline = time.time() + timeout_sec
    last: Any | None = None
    while time.time() < deadline:
        lobby_page: Any | None = None
        vendor_page: Any | None = None
        for p in context.pages:
            last = p
            if not _url_is_vendor(p.url):
                continue
            vendor_page = p
            if prefer_lobby and _is_lobby_url(p.url):
                lobby_page = p
        if lobby_page:
            return lobby_page
        if vendor_page:
            return vendor_page
        time.sleep(0.4)
    return last


def _iter_click_targets(page: Any) -> list[Any]:
    out: list[Any] = [page]
    try:
        out.extend(page.frames)
    except Exception:
        pass
    return out


def _table_health(page: Any, *, table_name: str, table_id: int) -> dict[str, Any]:
    best: dict[str, Any] | None = None
    try:
        targets: list[Any] = []
        try:
            targets.extend(_iter_click_targets(page))
        except Exception:
            pass
        targets.append(page)
        for frame in targets:
            try:
                out = frame.evaluate(
                    _JS_TABLE_HEALTH,
                    {"tableName": table_name, "tableId": table_id},
                )
            except Exception:
                continue
            if not isinstance(out, dict):
                continue
            if out.get("healthy"):
                return out
            if out.get("onTable") and (
                best is None or not best.get("onTable")
            ):
                best = out
        if best is not None:
            return best
        out = page.evaluate(
            _JS_TABLE_HEALTH,
            {"tableName": table_name, "tableId": table_id},
        )
        return out if isinstance(out, dict) else {"healthy": False}
    except Exception as exc:
        return {"healthy": False, "error": str(exc)}


def _page_on_lobby(page: Any) -> bool:
    if _is_lobby_url(page.url):
        return True
    try:
        for frame in _iter_click_targets(page):
            try:
                if frame.evaluate(_JS_IS_LOBBY):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _return_to_lobby(page: Any) -> bool:
    """Thoát trang bàn lỗi → về sảnh."""
    if _page_on_lobby(page) and not _is_table_url(page.url):
        return True
    print("  Quay lại sảnh vendor…", file=sys.stderr)
    for label in ("Tables", "Lobby", "Back", "返回", "Sảnh"):
        try:
            loc = page.get_by_text(label, exact=False).first
            if loc.count() and loc.is_visible(timeout=1500):
                loc.click(timeout=5000)
                page.wait_for_timeout(2500)
                if _page_on_lobby(page):
                    return True
        except Exception:
            pass
    try:
        page.go_back(wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(2500)
    except Exception:
        pass
    if _is_table_url(page.url) or not _page_on_lobby(page):
        origin = _vendor_origin(page.url)
        if origin:
            try:
                page.goto(
                    origin + "/player/",
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                page.wait_for_timeout(3500)
            except Exception:
                pass
    return _page_on_lobby(page) or not _is_table_url(page.url)


def _table_visible_on_lobby(
    page: Any, *, table_name: str, table_id: int
) -> bool:
    for frame in _iter_click_targets(page):
        try:
            out = frame.evaluate(
                _JS_TABLE_VISIBLE,
                {"tableName": table_name, "tableId": table_id},
            )
            if isinstance(out, dict) and out.get("visible"):
                return True
        except Exception:
            continue
    return False


def _wait_lobby(
    page: Any,
    *,
    table_name: str,
    table_id: int = DEFAULT_TABLE_ID,
    timeout_sec: float = 20,
) -> bool:
    """Chờ sảnh / thẻ bàn xuất hiện — poll nhanh, không sleep cố định."""
    deadline = time.time() + timeout_sec
    markers = (table_name, "C05", "C06", "Tables", "Baccarat", "Lobby")
    while time.time() < deadline:
        if _is_table_url(page.url):
            h = _table_health(page, table_name=table_name, table_id=table_id)
            if h.get("healthy"):
                return True
            _return_to_lobby(page)
        elif _table_visible_on_lobby(page, table_name=table_name, table_id=table_id):
            return True
        elif _page_on_lobby(page):
            return True
        else:
            try:
                body = page.evaluate(
                    "() => (document.body && document.body.innerText) || ''"
                )
                if isinstance(body, str) and any(m in body for m in markers):
                    return True
            except Exception:
                pass
        page.wait_for_timeout(280)
    return _page_on_lobby(page) or _table_visible_on_lobby(
        page, table_name=table_name, table_id=table_id
    )


def _wait_table_healthy(
    page: Any,
    *,
    table_name: str,
    table_id: int,
    max_sec: float = 8.0,
) -> dict[str, Any]:
    deadline = time.time() + max_sec
    last: dict[str, Any] = {"healthy": False}
    while time.time() < deadline:
        last = _table_health(page, table_name=table_name, table_id=table_id)
        if last.get("healthy"):
            return last
        page.wait_for_timeout(450)
    return last


def _try_playwright_click(page: Any, *, table_name: str, table_id: int) -> dict[str, Any]:
    if _is_table_url(page.url):
        return {"ok": False, "method": "playwright", "reason": "on_table_page"}

    exact_label = f"Baccarat {table_name}"
    patterns: list[tuple[str, Any]] = [
        ("exact_baccarat", exact_label),
        (f'[data-table-id="{table_id}"]', None),
        (f'[data-tableid="{table_id}"]', None),
    ]

    for frame in _iter_click_targets(page):
        for key, pat in patterns:
            try:
                if key == "exact_baccarat":
                    loc = frame.get_by_text(pat, exact=True)
                else:
                    loc = frame.locator(pat).first
                if loc.count() == 0:
                    continue
                loc.wait_for(state="visible", timeout=2000)
                loc.scroll_into_view_if_needed(timeout=2000)
                box = loc.bounding_box()
                if box:
                    mouse = getattr(frame, "page", page).mouse
                    mouse.click(
                        box["x"] + box["width"] / 2,
                        box["y"] + box["height"] / 2,
                    )
                else:
                    loc.click(timeout=8000)
                return {"ok": True, "method": f"playwright:{key}"}
            except Exception:
                continue

        try:
            loc = frame.get_by_text(
                re.compile(rf"^Baccarat\s+{re.escape(table_name)}\s*$", re.I)
            ).first
            if loc.count():
                loc.click(timeout=8000)
                return {"ok": True, "method": "playwright:regex_baccarat"}
        except Exception:
            pass

    return {"ok": False, "method": "playwright", "reason": "no_locator"}


def _try_js_click(page: Any, *, table_name: str, table_id: int) -> dict[str, Any]:
    for frame in _iter_click_targets(page):
        try:
            out = frame.evaluate(
                _JS_CLICK_LOBBY_TABLE,
                {"tableName": table_name, "tableId": table_id},
            )
            if isinstance(out, dict) and out.get("ok"):
                return {**out, "method": out.get("how") or "js"}
        except Exception:
            continue
    return {"ok": False, "method": "js", "reason": "not_found"}


def enter_vendor_table(
    page: Any,
    *,
    table_name: str = DEFAULT_TABLE_NAME,
    table_id: int = DEFAULT_TABLE_ID,
    timeout_sec: float = 90,
    wait_lobby_sec: float = 45,
    lobby_settle_sec: float = 5.0,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": False,
        "table_name": table_name,
        "table_id": table_id,
    }
    if page is None:
        out["error"] = "no_page"
        return out

    settle_sec = max(2.0, float(lobby_settle_sec))
    print(
        f"Chờ sảnh render ~{settle_sec:.0f}s → click bàn {table_name} (id {table_id})…",
        file=sys.stderr,
    )

    health = _table_health(page, table_name=table_name, table_id=table_id)
    if health.get("healthy"):
        out["ok"] = True
        out["method"] = "already_healthy"
        out["url"] = page.url
        print(f"  Đã ở bàn OK — {page.url[:90]}", file=sys.stderr)
        return out

    if _is_table_url(page.url) and (health.get("broken") or not health.get("healthy")):
        _return_to_lobby(page)
        page.wait_for_timeout(1200)

    if not _page_on_lobby(page) and not _table_visible_on_lobby(
        page, table_name=table_name, table_id=table_id
    ):
        _return_to_lobby(page)
        page.wait_for_timeout(800)

    lobby_deadline = time.time() + min(wait_lobby_sec, timeout_sec * 0.5)
    lobby_seen_at = 0.0
    while time.time() < lobby_deadline:
        h = _table_health(page, table_name=table_name, table_id=table_id)
        if h.get("healthy"):
            out["ok"] = True
            out["method"] = "already_healthy"
            out["url"] = page.url
            return out
        on_lobby = _page_on_lobby(page)
        card_visible = _table_visible_on_lobby(
            page, table_name=table_name, table_id=table_id
        )
        if on_lobby or card_visible:
            if lobby_seen_at <= 0:
                lobby_seen_at = time.time()
                print(
                    f"  Sảnh đã mở — đứng {settle_sec:.0f}s cho bàn {table_name} load…",
                    file=sys.stderr,
                )
            if time.time() - lobby_seen_at >= settle_sec and (card_visible or on_lobby):
                break
        page.wait_for_timeout(350)
    else:
        _wait_lobby(
            page,
            table_name=table_name,
            table_id=table_id,
            timeout_sec=min(15, wait_lobby_sec * 0.35),
        )
        if _page_on_lobby(page) and lobby_seen_at <= 0:
            lobby_seen_at = time.time()
            page.wait_for_timeout(int(settle_sec * 1000))

    deadline = time.time() + timeout_sec
    attempts: list[dict[str, Any]] = []

    while time.time() < deadline:
        health = _table_health(page, table_name=table_name, table_id=table_id)
        if health.get("healthy"):
            out["ok"] = True
            out["method"] = "already_healthy"
            out["url"] = page.url
            return out

        if _is_table_url(page.url) and health.get("broken"):
            _return_to_lobby(page)
            page.wait_for_timeout(1000)
            lobby_seen_at = time.time()
            continue

        if not _page_on_lobby(page) and not _table_visible_on_lobby(
            page, table_name=table_name, table_id=table_id
        ):
            _return_to_lobby(page)
            page.wait_for_timeout(800)
            continue

        card_visible = _table_visible_on_lobby(
            page, table_name=table_name, table_id=table_id
        )
        settled = lobby_seen_at > 0 and (time.time() - lobby_seen_at) >= settle_sec
        if not card_visible and not settled:
            page.wait_for_timeout(400)
            continue

        for fn in (_try_js_click, _try_playwright_click):
            r = fn(page, table_name=table_name, table_id=table_id)
            attempts.append(r)
            if not r.get("ok"):
                continue
            h = _wait_table_healthy(
                page, table_name=table_name, table_id=table_id, max_sec=8.0
            )
            attempts.append({"health": h})
            if h.get("healthy"):
                out["ok"] = True
                out["method"] = r.get("method") or r.get("how")
                out["url"] = page.url
                out["attempts"] = attempts[-10:]
                print(f"  OK vào bàn (sảnh → click) — {out['method']}", file=sys.stderr)
                return out
            if _is_table_url(page.url):
                print(
                    "  Click có phản hồi nhưng bàn lỗi — quay sảnh thử lại…",
                    file=sys.stderr,
                )
                _return_to_lobby(page)
                page.wait_for_timeout(1000)

        page.wait_for_timeout(400)

    out["error"] = "enter_table_timeout"
    out["url"] = getattr(page, "url", "")
    out["attempts"] = attempts[-10:]
    out["health"] = _table_health(page, table_name=table_name, table_id=table_id)
    print(
        f"  Không vào được bàn {table_name} — thử click tay trên sảnh.",
        file=sys.stderr,
    )
    return out


def enter_table_on_context(
    context: Any,
    *,
    table_name: str = DEFAULT_TABLE_NAME,
    table_id: int = DEFAULT_TABLE_ID,
    timeout_sec: float = 90,
) -> dict[str, Any]:
    page = find_vendor_page(context, timeout_sec=min(timeout_sec, 90), prefer_lobby=True)
    if not page:
        return {"ok": False, "error": "no_vendor_page"}
    return enter_vendor_table(
        page,
        table_name=table_name,
        table_id=table_id,
        timeout_sec=timeout_sec,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Vào bàn vendor (Chrome CDP đang mở game)")
    ap.add_argument("--table", default=DEFAULT_TABLE_NAME)
    ap.add_argument("--table-id", type=int, default=DEFAULT_TABLE_ID)
    ap.add_argument("--cdp", default=CDP_URL)
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("pip install playwright", file=sys.stderr)
        return 1

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(args.cdp)
        ctx = browser.contexts[0] if browser.contexts else None
        if not ctx:
            print(json.dumps({"ok": False, "error": "no_context"}))
            return 1
        out = enter_table_on_context(
            ctx,
            table_name=args.table.strip(),
            table_id=args.table_id,
        )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
