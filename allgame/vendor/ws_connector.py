# -*- coding: utf-8 -*-
"""Kết nối WS Game B (pipeline chung cho allgame)."""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlparse

from allgame.vendor.c168_vendor_flow import connect_c168_vendor_ws
from allgame.vendor.config import vendor_table_cfg

_PORTAL_SPECS: dict[str, dict[str, str]] = {
    "c168": {
        "hall": "https://af861c.c168f.com",
        "domain": "c168b2.cc",
        "sitecode": "2865",
        "origin": "https://c168b2.cc",
    },
    "f168": {
        "hall": "https://ah861f.f1sau8.com",
        "domain": "f1686s.com",
        "sitecode": "280",
        "origin": "https://f1686s.com",
    },
    "fly88": {
        "hall": "https://ok.fly88b.cc",
        "domain": "m.fly88t.vip",
        "sitecode": "2300",
        "origin": "https://m.fly88t.vip",
    },
}

_VENDOR_MARKERS = (
    "/player/webmain",
    "/player/singlebactable",
    "intplaynet.com/player",
    "bpcdf.",
    "tgmeq",
    "mhuxu",
    "vesnamex",
)

_JS_C168_GAME_LOGIN = """
async ({ hall, origin, domain, sitecode, platformId, categoryId, gameId }) => {
  const o = origin || location.origin || "";
  const d = domain || (new URL(o || location.href)).host || "";
  const sc = String(sitecode || "2865");
  const readToken = () => {
    try {
      const raw = localStorage.getItem("web__lobby__persisted__token");
      if (!raw) return null;
      return JSON.parse(decodeURIComponent(raw));
    } catch (e) {
      return null;
    }
  };
  const tokenBox = readToken();
  const infos = tokenBox && tokenBox.tokenInfos;
  if (!infos || !infos.session_key) {
    return { ok: false, error: "missing_session_key" };
  }
  const jwt = String(infos.jwt_token || "");
  if (jwt.length < 40) {
    return { ok: false, error: "missing_newjwt" };
  }
  let device = "";
  try {
    const d = localStorage.getItem("web__lobby__persisted__device");
    device = d ? JSON.parse(decodeURIComponent(d)).uuid || "" : "";
  } catch (e) {}
  const headers = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json",
    "origin": o,
    "referer": o + "/",
    "token": infos.session_key,
    "newjwt": jwt,
    "sitecode": sc,
    "domain": d,
    "currency": "VND",
    "device": device,
    "x-data-mode": "plain",
    "x-version": "7.3.17",
    "appversion": "v7.3.17",
    "x-device": "1-1",
    "timestamp": String(Math.floor(Date.now() / 1000)),
  };
  const now = Math.floor(Date.now() / 1000);
  try {
    await fetch(hall + "/hall/api/gameCenter/gameApi/logout", {
      method: "POST",
      headers,
      body: JSON.stringify({ os_type: 3, callContext: "allgame_ws_connect", time: now }),
    });
  } catch (e) {}
  await new Promise((r) => setTimeout(r, 450));
  const bodyLogin = {
    os_type: 3,
    gameid: gameId,
    platfromid: platformId,
    exitUrl: "",
    cid: "999998",
    time: now,
  };
  const resp = await fetch(hall + "/hall/api/gameCenter/gameApi/login", {
    method: "POST",
    headers,
    body: JSON.stringify(bodyLogin),
  });
  const text = await resp.text();
  let data = null;
  try { data = JSON.parse(text); } catch (e) {}
  if (!(resp.ok && data && data.code === 1)) {
    return { ok: false, error: "gameApi_login_fail", status: resp.status, body: text.slice(0, 300) };
  }
  const gameUrl =
    (data.data && (data.data.game_url || (data.data.url && data.data.url[0] && data.data.url[0].url))) ||
    "";
  if (!gameUrl) {
    return { ok: false, error: "no_game_url", body: text.slice(0, 300) };
  }
  return { ok: true, game_url: gameUrl };
}
"""

_JS_CLICK_LOBBY_TABLE = """
({ tableName, tableId }) => {
  const name = String(tableName || '').trim();
  const codeRe = name
    ? new RegExp('(?:Baccarat\\\\s+)?' + name + '(?:\\\\s|$|\\\\n)', 'i')
    : /baccarat\\s+[a-z0-9]+/i;
  const hitDoc = (doc) => {
    if (!doc) return null;
    if (Number(tableId || 0) > 0) {
      const byAttr = doc.querySelector(
        `[data-table-id="${tableId}"],[data-tableid="${tableId}"],[data-id="${tableId}"]`
      );
      if (byAttr) {
        byAttr.click();
        return { ok: true, how: 'attr' };
      }
    }
    const picks = [];
    for (const el of doc.querySelectorAll('div,li,a,span,td,section,article')) {
      const t = (el.textContent || '').replace(/\\s+/g, ' ').trim();
      if (!codeRe.test(t) && !/baccarat/i.test(t)) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 24 || r.height < 18) continue;
      const area = r.width * r.height;
      if (area <= 0 || area > 150000) continue;
      picks.push({ el, area, label: t.slice(0, 120) });
    }
    picks.sort((a, b) => a.area - b.area);
    if (picks.length) {
      picks[0].el.click();
      return { ok: true, how: 'text', label: picks[0].label };
    }
    return null;
  };
  let r = hitDoc(document);
  if (r) return r;
  for (const fr of document.querySelectorAll('iframe')) {
    try {
      r = hitDoc(fr.contentDocument);
      if (r) return { ...r, iframe: true };
    } catch (e) {}
  }
  return { ok: false, reason: 'not_found' };
}
"""

_JS_CLICK_LOBBY_BY_COORD = """
({ points }) => {
  const clickAt = (doc, x, y) => {
    if (!doc || !doc.elementFromPoint) return false;
    const el = doc.elementFromPoint(x, y);
    if (!el) return false;
    try {
      el.dispatchEvent(new MouseEvent("mousemove", { bubbles: true, clientX: x, clientY: y }));
      el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, clientX: x, clientY: y }));
      el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, clientX: x, clientY: y }));
      el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, clientX: x, clientY: y }));
      return true;
    } catch (e) {
      try { el.click(); return true; } catch (e2) { return false; }
    }
  };
  for (const p of points || []) {
    const x = Number(p.x || 0);
    const y = Number(p.y || 0);
    if (clickAt(document, x, y)) return { ok: true, how: "coord", x, y };
    for (const fr of document.querySelectorAll("iframe")) {
      try {
        const r = fr.getBoundingClientRect();
        if (x < r.left || x > r.right || y < r.top || y > r.bottom) continue;
        const fx = Math.max(1, Math.floor(x - r.left));
        const fy = Math.max(1, Math.floor(y - r.top));
        if (clickAt(fr.contentDocument, fx, fy)) return { ok: true, how: "coord_iframe", x, y, fx, fy };
      } catch (e) {}
    }
  }
  return { ok: false, reason: "coord_not_clicked" };
}
"""

_JS_TABLE_HEALTH = """
({ tableName, tableId }) => {
  const u = (location.href || '').toLowerCase();
  const body = (document.body && document.body.innerText) || '';
  const tname = String(tableName || 'C06');
  const onTable = u.includes('singlebactable');
  const broken =
    onTable &&
    (body.includes('Dealer: --') || body.includes('Dealer:--')) &&
    !body.includes('Baccarat ' + tname) &&
    !body.includes('PLAYER');
  const goodOnTable =
    onTable &&
    (body.includes('Baccarat ' + tname) ||
      (body.includes(tname) && (body.includes('PLAYER') || body.includes('Confirm'))));
  const chips = /\\b10\\b/.test(body) && /\\b20\\b/.test(body);
  return {
    healthy: Boolean(goodOnTable && !broken && chips),
    broken,
    onTable,
    url: location.href.slice(0, 180),
    hasTableName: body.includes(tname),
    hasPlayer: body.includes('PLAYER')
  };
}
"""

_JS_TRY_RELOAD_IN_TABLE = """
() => {
  const labels = ['Reload', 'reload', 'Tải lại'];
  for (const txt of labels) {
    const nodes = Array.from(document.querySelectorAll('button,a,div,span'));
    for (const n of nodes) {
      const t = (n.textContent || '').trim();
      if (!t || t.length > 20) continue;
      if (t === txt) {
        try { n.click(); return { ok: true, how: 'text', text: t }; } catch (e) {}
      }
    }
  }
  return { ok: false, reason: 'reload_button_not_found' };
}
"""

_JS_IS_SESSION_TIMEOUT = """
() => {
  const body = ((document.body && document.body.innerText) || "").toLowerCase();
  const title = (document.title || "").toLowerCase();
  const text = body + "\\n" + title;
  return (
    text.includes("session timeout") ||
    text.includes("session timeout. auto logout") ||
    text.includes("auto logout") ||
    text.includes("please login again")
  );
}
"""

_JS_ENTER_TABLE_API = """
async ({ tableId }) => {
  const tid = String(tableId || "");
  const postForm = async (path, formBody, refererPath = "") => {
    const headers = {
      "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
      "X-Requested-With": "XMLHttpRequest",
    };
    if (refererPath) {
      headers["Referer"] = location.origin + refererPath;
    }
    const resp = await fetch(path, {
      method: "POST",
      headers,
      credentials: "include",
      body: formBody,
    });
    return { ok: resp.ok, status: resp.status, text: (await resp.text()).slice(0, 200) };
  };
  const fireAndForget = async (path, formBody, refererPath = "") => {
    try {
      return await postForm(path, formBody, refererPath);
    } catch (e) {
      return { ok: false, status: 0, text: String(e).slice(0, 180) };
    }
  };
  try {
    const setOut = await postForm(
      "/player/update/setUserSingleTableID",
      "singleTableID=" + encodeURIComponent(tid),
      "/player/webMain.jsp?dm=1&title=1"
    );
    // Vendor build khác nhau: thử lần lượt payload đã bắt được từ thao tác tay.
    const chooseBodies = [
      "queryTableID=" + encodeURIComponent(tid),
      "tableID=" + encodeURIComponent(tid),
      "hallType=1&tableID=" + encodeURIComponent(tid),
      "hallType=1&queryTableID=" + encodeURIComponent(tid),
    ];
    let chooseOut = { ok: false, status: 0, text: "" };
    let chooseBodyUsed = "";
    for (const b of chooseBodies) {
      const out = await postForm(
        "/player/query/chooseSingleTableChannel",
        b,
        "/player/singleBacTable.jsp?dm=1"
      );
      if (out.ok) {
        chooseOut = out;
        chooseBodyUsed = b;
        break;
      }
      if (!chooseOut.ok) {
        chooseOut = out;
        chooseBodyUsed = b;
      }
    }
    // Batch warm-up theo pattern thực tế sau click vào bàn.
    const initOut = await fireAndForget("/player/query/queryInitTableInfo", "tableID=" + encodeURIComponent(tid));
    await new Promise((r) => setTimeout(r, 120));
    const betLimit1 = await fireAndForget("/player/query/queryBetLimit", "tableID=" + encodeURIComponent(tid));
    await new Promise((r) => setTimeout(r, 90));
    const tx1 = await fireAndForget("/player/query/queryTransactions", "");
    await new Promise((r) => setTimeout(r, 90));
    const betLimit2 = await fireAndForget("/player/query/queryBetLimit", "tableID=" + encodeURIComponent(tid));
    await new Promise((r) => setTimeout(r, 90));
    const chatOut = await fireAndForget("/player/query/getChatToken", "");
    await new Promise((r) => setTimeout(r, 90));
    const betModeOut = await fireAndForget(
      "/player/query/queryBacTableBetMode",
      "tableID=" + encodeURIComponent(tid) + "&gameShoe=0&gameRound=0"
    );
    await new Promise((r) => setTimeout(r, 90));
    const changeSetting1 = await fireAndForget("/player/update/changePlayerSetting", "tableID=" + encodeURIComponent(tid));
    await new Promise((r) => setTimeout(r, 90));
    const tx2 = await fireAndForget("/player/query/queryTransactions", "");
    await new Promise((r) => setTimeout(r, 90));
    const secUrlOut = await fireAndForget("/player/query/getSecurityUrl", "");
    await new Promise((r) => setTimeout(r, 90));
    const changeSetting2 = await fireAndForget("/player/update/changePlayerSetting", "tableID=" + encodeURIComponent(tid));
    return {
      ok: !!(setOut.ok && chooseOut.ok),
      step: setOut.ok && chooseOut.ok ? "setUserSingleTableID+chooseSingleTableChannel" : "enter_table_api_failed",
      setOut,
      chooseOut,
      chooseBodyUsed,
      initOut,
      betLimit1,
      betLimit2,
      tx1,
      tx2,
      chatOut,
      betModeOut,
      secUrlOut,
      changeSetting1,
      changeSetting2,
    };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}
"""


def connect_vendor_ws(
    account: dict[str, Any],
    *,
    chrome: dict[str, Any],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    portal_id = str(account.get("portal_id") or "").strip().lower()
    if portal_id == "c168":
        return connect_c168_vendor_ws(account, chrome=chrome, cfg=cfg)
    if portal_id in _PORTAL_SPECS:
        return _connect_game_vendor_ws(account, chrome=chrome, cfg=cfg)
    return {"ok": False, "error": "ws_not_implemented_for_portal", "portal_id": portal_id}


def _is_vendor_url(url: str) -> bool:
    u = str(url or "").lower()
    return any(m in u for m in _VENDOR_MARKERS)


def _connect_game_vendor_ws(
    account: dict[str, Any],
    *,
    chrome: dict[str, Any],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    cdp_url = str(chrome.get("cdp_url") or "").strip()
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
    vc = vendor_table_cfg(cfg)
    spec = _PORTAL_SPECS.get(str(account.get("portal_id") or "").strip().lower(), {})
    platform_id = int(vc.get("platform_id") or 1012)
    category_id = int(vc.get("category_id") or 4)
    table_name = str(vc.get("table_name") or "C06")
    table_id = int(vc.get("table_id") or 1006)
    game_id = platform_id * 10_000
    ws_urls: list[str] = []
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            return {"ok": False, "error": f"connect_cdp_failed:{e}"}
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        pages = list(context.pages or [])
        page = pages[0] if pages else context.new_page()
        for pg in pages:
            u = str(pg.url or "").lower()
            if _is_vendor_url(u):
                page = pg
                break
        home_origin = str(spec.get("origin") or "")
        if home_origin and not _is_vendor_url(str(page.url or "").lower()):
            try:
                page.goto(home_origin, wait_until="domcontentloaded", timeout=120_000)
            except Exception:
                pass
            page.wait_for_timeout(1200)
        cur_url = str(page.url or "")
        cur_low = cur_url.lower()
        on_vendor = _is_vendor_url(cur_low)
        session_timed_out = False
        if on_vendor:
            try:
                session_timed_out = bool(page.evaluate(_JS_IS_SESSION_TIMEOUT))
            except Exception:
                session_timed_out = False
        if session_timed_out:
            on_vendor = False
        game_url = ""
        if on_vendor:
            # Đã ở vendor thì không login lại để tránh nhảy vòng về game/lobby khác.
            game_url = cur_url
        else:
            if home_origin:
                # Timeout/vendor stale => quay về lobby portal để lấy lại token localStorage mới nhất.
                try:
                    page.goto(home_origin, wait_until="domcontentloaded", timeout=120_000)
                    page.wait_for_timeout(1200)
                except Exception:
                    pass
            out = page.evaluate(
                _JS_C168_GAME_LOGIN,
                {
                    "hall": str(spec.get("hall") or ""),
                    "origin": str(spec.get("origin") or ""),
                    "domain": str(spec.get("domain") or ""),
                    "sitecode": str(spec.get("sitecode") or ""),
                    "platformId": platform_id,
                    "categoryId": category_id,
                    "gameId": game_id,
                },
            )
            if not isinstance(out, dict) or not out.get("ok"):
                return {"ok": False, "error": "open_game_b_failed", "detail": out}
            game_url = str(out.get("game_url") or "")
            if not game_url:
                return {"ok": False, "error": "empty_game_url"}
        table_origin = ""
        for cand in (str(page.url or ""), game_url):
            low = cand.lower()
            p = urlparse(cand)
            if not (p.scheme and p.netloc):
                continue
            if "/player/" in low or any(m in low for m in _VENDOR_MARKERS):
                table_origin = f"{p.scheme}://{p.netloc}"
                break
        if not table_origin:
            table_origin = str(spec.get("origin") or "").rstrip("/")

        ws_hit = {"ok": False}
        ws_table_frame_hit = {"ok": False}
        ws_table_session_hit = {"ok": False}
        ws_streaming_info_hit = {"ok": False}
        ws_table_session_sample = {"text": ""}
        ws_new_round_event = {"text": ""}
        ws_round_result_event = {"text": ""}
        ws_frame_samples: list[str] = []

        table_id_s = str(table_id)
        table_name_s = str(table_name or "").strip().lower()
        def _frame_has_table_signal(payload: str) -> bool:
            t = str(payload or "").lower()
            if not t:
                return False
            return any(
                k in t
                for k in (
                    f'"tableid":{table_id_s}',
                    f'"table_id":{table_id_s}',
                    f"tableid={table_id_s}",
                    f"table_id={table_id_s}",
                )
            )

        def _frame_has_table_session(payload: str) -> bool:
            t = str(payload or "").lower()
            if not t:
                return False
            has_table = _frame_has_table_signal(t)
            if not has_table:
                return False
            # "Vào bàn thật" cho mục tiêu nghe WS đặt cược: phải có thông tin phiên bàn.
            has_round = any(k in t for k in ('"gameround"', "gameround", '"game_round"', "game_round"))
            has_shoe = any(k in t for k in ('"gameshoe"', "gameshoe", '"game_shoe"', "game_shoe"))
            # Ưu tiên frame bàn thực sự (GameInfo) nếu có.
            has_gameinfo = ("messagetype" in t and "gameinfo" in t) or ("eventtype" in t and "gp_" in t)
            return bool(has_round and has_shoe and (has_gameinfo or has_table))

        def _frame_has_streaming_info(payload: str) -> bool:
            t = str(payload or "").lower()
            if "streaminginfo" not in t:
                return False
            # tableId thường dạng "C06", streamingName dạng "BTCB06"
            if table_name_s and f'"tableid":"{table_name_s}"' in t:
                return True
            digits = "".join(ch for ch in table_name_s if ch.isdigit())
            if digits and f'"streamingname":"btcb{digits}"' in t:
                return True
            return False

        def _parse_ws_json(payload: str) -> dict[str, Any] | None:
            s = str(payload or "")
            i = s.find("{")
            j = s.rfind("}")
            if i < 0 or j <= i:
                return None
            try:
                obj = json.loads(s[i : j + 1])
            except Exception:
                return None
            return obj if isinstance(obj, dict) else None

        def _on_ws(ws):
            try:
                u = str(ws.url or "")
            except Exception:
                u = ""
            if u:
                ws_urls.append(u)
                ws_hit["ok"] = True
            ws_url_low = u.lower()
            if "h54uk" not in ws_url_low:
                return

            def _on_frame(payload):
                try:
                    txt = str(getattr(payload, "payload", payload) or "")
                except Exception:
                    txt = ""
                if not txt:
                    return
                if len(ws_frame_samples) < 6:
                    ws_frame_samples.append(txt[:220])
                if _frame_has_table_signal(txt):
                    ws_table_frame_hit["ok"] = True
                if _frame_has_table_session(txt):
                    ws_table_session_hit["ok"] = True
                    if not ws_table_session_sample.get("text"):
                        ws_table_session_sample["text"] = txt[:260]
                if _frame_has_streaming_info(txt):
                    ws_streaming_info_hit["ok"] = True
                    if not ws_table_session_sample.get("text"):
                        ws_table_session_sample["text"] = txt[:260]
                obj = _parse_ws_json(txt)
                if isinstance(obj, dict):
                    msg = obj.get("message")
                    if isinstance(msg, dict):
                        try:
                            t_id = int(msg.get("tableID") or 0)
                        except Exception:
                            t_id = 0
                        if t_id == int(table_id):
                            ev = str(msg.get("eventType") or "")
                            if ev == "GP_NEW_GAME_START" and not ws_new_round_event.get("text"):
                                ws_new_round_event["text"] = txt[:320]
                            if ev in {"GP_WINNER", "GP_RESULT"} and not ws_round_result_event.get("text"):
                                ws_round_result_event["text"] = txt[:320]

            try:
                ws.on("framereceived", _on_frame)
            except Exception:
                pass

        # Đi trên tab hiện tại để chắc chắn UI rời sảnh vào game.
        page.on("websocket", _on_ws)
        if not on_vendor:
            page.goto(game_url, wait_until="domcontentloaded", timeout=120_000)
            # Một số vendor mở tab mới sau login; ưu tiên chuyển sang tab vendor thật để dùng đúng cookie mới.
            for pg in list(context.pages or []):
                if _is_vendor_url(str(pg.url or "")):
                    page = pg
                    break
        try:
            page.bring_to_front()
        except Exception:
            pass
        # Theo yêu cầu: đợi sảnh 5s cho load ổn định rồi mới gọi API vào bàn.
        page.wait_for_timeout(5000)
        def _recover_vendor_page():
            for pg in list(context.pages or []):
                try:
                    u = str(pg.url or "").lower()
                except Exception:
                    continue
                if _is_vendor_url(u) or "singlebactable" in u:
                    return pg
            return page

        def _find_table_page():
            for pg in list(context.pages or []):
                try:
                    u = str(pg.url or "").lower()
                except Exception:
                    continue
                if "singlebactable" in u:
                    return pg
            return None

        def _coord_points() -> list[dict[str, int]]:
            try:
                vp = page.viewport_size or {}
                w = int(vp.get("width") or 1366)
                h = int(vp.get("height") or 768)
            except Exception:
                w, h = 1366, 768
            # Vùng bàn ở nửa dưới/phải theo ảnh thực tế.
            xs = [int(w * k) for k in (0.62, 0.74, 0.86)]
            ys = [int(h * k) for k in (0.66, 0.76, 0.85)]
            return [{"x": x, "y": y} for y in ys for x in xs]

        def _mouse_click_grid() -> dict[str, Any]:
            points = _coord_points()
            for pnt in points:
                try:
                    page.mouse.click(pnt["x"], pnt["y"])
                    page.wait_for_timeout(260)
                    tp = _find_table_page()
                    if tp is not None:
                        return {"ok": True, "how": "mouse_grid_table_page", "x": pnt["x"], "y": pnt["y"]}
                except Exception:
                    continue
            return {"ok": False, "reason": "mouse_grid_failed"}

        # Từ sảnh vendor -> ưu tiên gọi API vào bàn theo flow thật.
        enter_deadline = time.time() + 22.0
        enter_ok = False
        enter_method = ""
        while time.time() < enter_deadline:
            try:
                # Nếu đang mắc ở trang bàn đen/lỗi thì quay lại lobby để click flow thật.
                h0 = page.evaluate(_JS_TABLE_HEALTH, {"tableName": table_name, "tableId": table_id})
            except Exception:
                h0 = {}
            if isinstance(h0, dict) and h0.get("onTable") and not h0.get("healthy"):
                try:
                    page.goto(
                        table_origin + "/player/webMain.jsp?dm=1&title=1",
                        wait_until="domcontentloaded",
                        timeout=120_000,
                    )
                    page.wait_for_timeout(1000)
                except Exception:
                    pass
            try:
                api_out = page.evaluate(_JS_ENTER_TABLE_API, {"tableId": table_id})
            except Exception:
                api_out = {"ok": False}
            if isinstance(api_out, dict) and api_out.get("ok"):
                enter_method = "api_setUserSingleTableID+chooseSingleTableChannel"
                try:
                    # Không mở thẳng URL bàn; flow thật giữ ở webMain và join bàn qua state/ws.
                    page.wait_for_timeout(900)
                except Exception:
                    page = _recover_vendor_page()
            try:
                h = page.evaluate(_JS_TABLE_HEALTH, {"tableName": table_name, "tableId": table_id})
            except Exception:
                page = _recover_vendor_page()
                h = {}
            if isinstance(h, dict) and h.get("healthy"):
                enter_ok = True
                if not enter_method:
                    enter_method = "already_on_table"
                break
            try:
                click_out = page.evaluate(
                    _JS_CLICK_LOBBY_TABLE,
                    {"tableName": table_name, "tableId": table_id},
                )
            except Exception:
                click_out = {"ok": False}
            if not (isinstance(click_out, dict) and click_out.get("ok")):
                try:
                    click_out = page.evaluate(
                        _JS_CLICK_LOBBY_TABLE,
                        {"tableName": "", "tableId": 0},
                    )
                except Exception:
                    click_out = {"ok": False}
            if not (isinstance(click_out, dict) and click_out.get("ok")):
                try:
                    click_out = page.evaluate(_JS_CLICK_LOBBY_BY_COORD, {"points": _coord_points()})
                except Exception:
                    click_out = {"ok": False}
            if not (isinstance(click_out, dict) and click_out.get("ok")):
                click_out = _mouse_click_grid()
            if isinstance(click_out, dict) and click_out.get("ok"):
                try:
                    page.wait_for_timeout(1000)
                except Exception:
                    page = _recover_vendor_page()
                tp = _find_table_page()
                if tp is not None:
                    page = tp
                    try:
                        page.bring_to_front()
                    except Exception:
                        pass
                try:
                    h2 = page.evaluate(_JS_TABLE_HEALTH, {"tableName": table_name, "tableId": table_id})
                except Exception:
                    page = _recover_vendor_page()
                    h2 = {}
                if isinstance(h2, dict) and h2.get("healthy"):
                    enter_ok = True
                    enter_method = str(click_out.get("how") or "click")
                    break
            try:
                page.wait_for_timeout(500)
            except Exception:
                page = _recover_vendor_page()

        deadline = time.time() + 20.0
        while time.time() < deadline:
            if ws_hit.get("ok"):
                break
            page.wait_for_timeout(300)
        has_table_ws = any("h54uk" in str(u).lower() for u in ws_urls)
        ws_connected = bool(ws_hit.get("ok") and has_table_ws)
        ready = bool(
            ws_connected
            and ws_table_frame_hit.get("ok")
            and (ws_table_session_hit.get("ok") or ws_streaming_info_hit.get("ok"))
        )
        # Không cho pass chỉ vì mở được URL ws h54uk; cần xác nhận đã vào bàn thật qua UI health.
        if enter_ok:
            try:
                h3 = page.evaluate(_JS_TABLE_HEALTH, {"tableName": table_name, "tableId": table_id})
            except Exception:
                h3 = {}
            if not (isinstance(h3, dict) and h3.get("healthy")):
                enter_ok = False
                if not enter_method:
                    enter_method = "table_health_not_ready"
        # Fallback theo mục tiêu thật: có frame phiên của đúng bàn trong WS => coi là đã vào bàn để nghe/cược.
        # Không phụ thuộc đổi URL sang singleBacTable vì click thật có thể vẫn ở webMain.
        page_url_now = str(page.url or "")
        page_url_low = page_url_now.lower()
        h4 = {}
        try:
            h4 = page.evaluate(_JS_TABLE_HEALTH, {"tableName": table_name, "tableId": table_id})
        except Exception:
            h4 = {}
        if isinstance(h4, dict) and h4.get("broken"):
            try:
                page.evaluate(_JS_TRY_RELOAD_IN_TABLE)
                page.wait_for_timeout(1200)
                h5 = page.evaluate(_JS_TABLE_HEALTH, {"tableName": table_name, "tableId": table_id})
            except Exception:
                h5 = {}
            if not (isinstance(h5, dict) and h5.get("healthy")):
                enter_ok = False
                enter_method = "black_screen_reload_failed"
        ws_session_ready = bool(ws_table_session_hit.get("ok"))
        if not enter_ok and ws_session_ready and not (isinstance(h4, dict) and h4.get("broken")):
            enter_ok = True
            if not enter_method or enter_method == "table_health_not_ready":
                enter_method = "ws_table_session"
        return {
            "ok": bool(ready and enter_ok),
            "ready_to_bet": bool(ready and enter_ok),
            "ws_connected": ws_connected,
            "ws_table_frame_ok": bool(ws_table_frame_hit.get("ok")),
            "ws_table_session_ok": bool(ws_table_session_hit.get("ok")),
            "ws_streaming_info_ok": bool(ws_streaming_info_hit.get("ok")),
            "ws_table_session_sample": str(ws_table_session_sample.get("text") or ""),
            "ws_new_round_event": str(ws_new_round_event.get("text") or ""),
            "ws_round_result_event": str(ws_round_result_event.get("text") or ""),
            "enter_table_ok": enter_ok,
            "enter_table_method": enter_method,
            "table_name": table_name,
            "table_id": table_id,
            "ws_urls": ws_urls[:5],
            "game_url": game_url[:180],
            "platform_id": platform_id,
            "category_id": category_id,
            "final_page_url": page_url_now,
            "ws_frame_samples": ws_frame_samples,
        }
