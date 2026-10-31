# -*- coding: utf-8 -*-
"""
Bắt đúng request khi nhận lì xì phòng chat bằng tay.

Mở Chrome (headed) với session 1 nick, hook mọi /chat/grabred + vue dispatch.
Anh bấm nhận tay trên cửa sổ → script ghi header/body/response ra file.
Không tự gọi grab.

  python sniff_chat_lixi_claim.py
  python sniff_chat_lixi_claim.py bongtuyetty
  python sniff_chat_lixi_claim.py --path /mobile/
  python sniff_chat_lixi_claim.py --path /home/ --profile pc
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DIR))

OUT_DIR = DIR / "red_packet_logs"
DEFAULT_PATH = "/mobile/"

HOOK_JS = r"""
(() => {
  if (window.__chatLixiHook) return true;
  window.__chatLixiHook = true;
  window.__chatLixiLogs = [];
  const push = (row) => {
    try { window.__chatLixiLogs.push(Object.assign({ t: Date.now() }, row)); } catch (e) {}
  };
  const hit = (url, action) => {
    const u = String(url || '');
    const a = String(action || '');
    return /grabred|chatGrabred|getredpacket|red_notify|chat\/getChatLog|chat\/getToken/i.test(u + a);
  };
  const ofetch = window.fetch;
  if (ofetch) {
    window.fetch = async function(input, init) {
      const url = String(typeof input === 'string' ? input : (input && input.url) || '');
      const method = ((init && init.method) || (input && input.method) || 'GET') + '';
      let body = init && init.body;
      let headers = null;
      try {
        if (init && init.headers) {
          if (init.headers.forEach) {
            headers = {};
            init.headers.forEach((v, k) => { headers[k] = v; });
          } else headers = Object.assign({}, init.headers);
        }
      } catch (e) {}
      if (hit(url, '')) push({
        kind: 'fetch', method, url,
        headers,
        body: body == null ? null : String(body).slice(0, 2000),
      });
      const resp = await ofetch.apply(this, arguments);
      if (hit(url, '')) {
        let rh = {};
        try { resp.headers.forEach((v, k) => { rh[k] = v; }); } catch (e) {}
        try {
          const text = await resp.clone().text();
          push({
            kind: 'fetch_resp', method, url, status: resp.status,
            resp_headers: rh,
            body: String(text || '').slice(0, 2000),
          });
        } catch (e) {
          push({ kind: 'fetch_resp', method, url, status: resp.status, error: String(e) });
        }
      }
      return resp;
    };
  }
  const XO = XMLHttpRequest.prototype.open;
  const XS = XMLHttpRequest.prototype.send;
  const XH = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.open = function(m, u) {
    this.__lixi = { method: m, url: u, headers: {} };
    return XO.apply(this, arguments);
  };
  XMLHttpRequest.prototype.setRequestHeader = function(k, v) {
    try { if (this.__lixi) this.__lixi.headers[k] = v; } catch (e) {}
    return XH.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function(body) {
    const meta = this.__lixi || {};
    if (hit(meta.url || '', '')) {
      push({
        kind: 'xhr', method: meta.method, url: meta.url,
        headers: meta.headers || {},
        body: body == null ? null : String(body).slice(0, 2000),
      });
      this.addEventListener('load', function() {
        push({
          kind: 'xhr_resp', method: meta.method, url: meta.url,
          status: this.status,
          body: String(this.responseText || '').slice(0, 2000),
        });
      });
    }
    return XS.apply(this, arguments);
  };
  const hookStore = () => {
    const app = document.querySelector('#app');
    const vm = app && app.__vue__;
    if (!vm || !vm.$store || !vm.$store.dispatch || vm.$store.__chatLixi) return !!vm;
    const orig = vm.$store.dispatch.bind(vm.$store);
    vm.$store.__chatLixi = true;
    vm.$store.dispatch = function(action, payload) {
      const name = String(action || '');
      if (hit('', name) || /chat\//i.test(name)) {
        push({ kind: 'vue_dispatch', action: name, payload });
      }
      return orig(action, payload);
    };
    push({ kind: 'vue_hooked' });
    return true;
  };
  hookStore();
  setInterval(hookStore, 1500);
  push({ kind: 'hook_ready' });
  return true;
})();
"""


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?", default="", help="username hoặc acc id; trống = nick đầu roster")
    ap.add_argument(
        "--profile",
        default="android",
        help="Mặc định android (mobile). pc | android | android_app | ios",
    )
    ap.add_argument(
        "--path",
        default=DEFAULT_PATH,
        help="Path mở site. Mặc định /mobile/ (vd. https://host/mobile/)",
    )
    ap.add_argument("--wait-sec", type=int, default=300, help="Chờ anh bấm nhận (mặc định 5 phút)")
    args = ap.parse_args()

    from xoso66_accounts_db import get_account, get_account_by_username
    from xoso66_deposit import apply_response_tokens
    from xoso66_game_domain import resolve_base_url, site_host
    from xoso66_playwright_ctx import _playwright_thread_setup, playwright_proxy
    from xoso66_proxy import ensure_proxy
    from xoso66_red_packet import CLIENT_PROFILES, _client_init_script, apply_client_profile
    from xoso66_session import ensure_session, merge_playwright_cookies

    target = (args.target or "").strip()
    if not target:
        roster = json.loads((DIR / "chat_lixi_trial_users.json").read_text(encoding="utf-8"))
        target = roster["users"][0]["username"]

    row = get_account_by_username(target) or get_account(target)
    if not row:
        print(f"Khong tim thay: {target}", flush=True)
        return 2
    aid = str(row["id"])
    user = str(row.get("username") or aid)
    session = ensure_session(aid)
    client_meta = apply_client_profile(session, args.profile) or CLIENT_PROFILES.get("android")
    proxy_str = ensure_proxy(session)
    base = resolve_base_url(session).rstrip("/")
    path = str(args.path or DEFAULT_PATH).strip() or DEFAULT_PATH
    if not path.startswith("/"):
        path = "/" + path
    if not path.endswith("/"):
        path = path + "/"
    open_url = f"{base}{path}"
    host = site_host(session)
    out = OUT_DIR / f"chat_lixi_sniff_{user}_{int(time.time())}.jsonl"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    use_pc = str(args.profile or "").strip().lower() in ("pc", "pc_baseline")
    # /mobile/ luôn dùng khung điện thoại để thao tác.
    use_mobile_ui = (not use_pc) or path.startswith("/mobile")

    def log(step: str, payload: dict) -> None:
        rec = {"t": time.time(), "step": step, **payload}
        with open(out, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        print(f"[{step}] {json.dumps(payload, ensure_ascii=False, default=str)[:1000]}", flush=True)

    log(
        "start",
        {
            "user": user,
            "aid": aid,
            "profile": args.profile,
            "url": open_url,
            "out": str(out),
        },
    )

    _playwright_thread_setup()
    from playwright.sync_api import sync_playwright

    px = playwright_proxy(proxy_str)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, proxy=px)
        ctx_kw: dict = {
            "user_agent": str(session.get("user_agent") or ""),
            "is_mobile": use_mobile_ui,
            "has_touch": use_mobile_ui,
        }
        if use_mobile_ui:
            ctx_kw["viewport"] = {"width": 412, "height": 915}
            ctx_kw["device_scale_factor"] = 2.625
        else:
            ctx_kw["no_viewport"] = True
        context = browser.new_context(**ctx_kw)
        cookies = [
            {"name": str(n), "value": str(v), "domain": host, "path": "/"}
            for n, v in (session.get("cookies") or {}).items()
            if v is not None
        ]
        if cookies:
            context.add_cookies(cookies)
        context.add_init_script(HOOK_JS)
        if client_meta:
            context.add_init_script(_client_init_script(client_meta))
        page = context.new_page()

        def on_request(req) -> None:
            url = req.url or ""
            if not re.search(r"grabred|getredpacket|getChatLog|getToken", url, re.I):
                return
            log(
                "pw_request",
                {
                    "method": req.method,
                    "url": url,
                    "headers": dict(req.headers),
                    "post_data": (req.post_data or "")[:2000],
                },
            )

        def on_response(resp) -> None:
            url = resp.url or ""
            if not re.search(r"grabred|getredpacket|getChatLog|getToken", url, re.I):
                return
            apply_response_tokens(session, resp.headers)
            body = ""
            try:
                body = resp.text()[:2000]
            except Exception as e:
                body = f"<err {e}>"
            log(
                "pw_response",
                {
                    "method": resp.request.method,
                    "url": url,
                    "status": resp.status,
                    "headers": dict(resp.headers),
                    "body": body,
                },
            )

        page.on("request", on_request)
        page.on("response", on_response)
        log("goto", {"msg": f"[1] goto {open_url} (commit, 60s)"})
        try:
            page.goto(open_url, wait_until="commit", timeout=60_000)
        except Exception as e:
            log("goto", {"msg": f"[1] commit fail — {str(e).splitlines()[0][:180]}"})
            raise
        try:
            page.wait_for_load_state("domcontentloaded", timeout=20_000)
        except Exception as e:
            log("goto", {"msg": f"[1] DCL chua xong — {str(e).splitlines()[0][:140]}"})
        page.wait_for_timeout(4000)
        page.evaluate(HOOK_JS)
        print(
            f"\n>>> Chrome mobile UI ({user}, profile={args.profile}).\n"
            f">>> URL: {open_url}\n"
            f">>> Anh tu mo Phong chat → bam nhan li xi trong {args.wait_sec}s.\n"
            f">>> Log: {out}\n"
            f">>> Ctrl+C de dung som.\n",
            flush=True,
        )
        deadline = time.time() + max(30, int(args.wait_sec))
        last_n = 0
        while time.time() < deadline:
            try:
                logs = page.evaluate("() => window.__chatLixiLogs || []") or []
            except Exception as e:
                log("eval_err", {"error": str(e)})
                break
            if len(logs) > last_n:
                for row in logs[last_n:]:
                    log("hook", row if isinstance(row, dict) else {"raw": row})
                last_n = len(logs)
                # Nếu đã thấy grabred response thì có thể kết thúc sớm
                if any(
                    isinstance(r, dict)
                    and r.get("kind") in ("fetch_resp", "xhr_resp")
                    and "grabred" in str(r.get("url") or "").lower()
                    for r in logs
                ):
                    print(">>> Da bat duoc grabred. Dong sau 5s.", flush=True)
                    page.wait_for_timeout(5000)
                    break
            page.wait_for_timeout(1000)
        try:
            merge_playwright_cookies(session, context)
        except Exception:
            pass
        browser.close()
    print(f"DONE out={out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
