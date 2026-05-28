# -*- coding: utf-8 -*-
"""
Gọi API hall C168 từ trong browser (Playwright evaluate).

Body vẫn được mã hóa bởi client JS của site — không cần reverse AES ra Python.
"""
from __future__ import annotations

import re
import sys
from typing import Any
from urllib.parse import urlparse

API_VERIFY_WITHDRAW_PASS = "/hall/api/member/user/security/verifyWithdrawPass"
API_MODIFY_WITHDRAW = "/hall/api/member/user/security/modifyWithdrawPass"

_JS_CALL_WITHDRAW_API = """
async ({ pin, verifyPath, modifyPath }) => {
  const PIN = String(pin || '').replace(/\\D/g, '').slice(0, 6);
  if (PIN.length !== 6) return { ok: false, error: 'pin_must_be_6_digits' };

  const app = document.querySelector('#app')?.__vue_app__;
  const gp = app?.config?.globalProperties || {};
  const pinia = gp.$pinia;

  const apiBase = (() => {
    const urls = performance.getEntriesByType('resource')
      .map(e => e.name)
      .filter(u => u.includes('/hall/api/'));
    for (let i = urls.length - 1; i >= 0; i--) {
      try { return new URL(urls[i]).origin; } catch (e) {}
    }
    for (const k of ['apiUrl', 'API_URL', 'hallApi', 'baseApi']) {
      const v = localStorage.getItem(k) || sessionStorage.getItem(k);
      if (v && v.startsWith('http')) return v.replace(/\\/+$/, '');
    }
    return '';
  })();

  const httpClients = [];
  const seen = new Set();
  const addClient = (obj, label) => {
    if (!obj || typeof obj !== 'object' || seen.has(obj)) return;
    if (typeof obj.post === 'function' || typeof obj.request === 'function') {
      seen.add(obj);
      httpClients.push({ label, client: obj });
    }
  };

  for (const k of ['$http', '$api', '$request', 'http', 'api', 'ajax', '$ajax']) {
    addClient(gp[k], 'gp.' + k);
  }
  if (pinia?._s) {
    for (const [sid, store] of Object.entries(pinia._s)) {
      addClient(store?.http, 'pinia.' + sid + '.http');
      addClient(store?.api, 'pinia.' + sid + '.api');
      addClient(store?.request, 'pinia.' + sid + '.request');
      addClient(store, 'pinia.' + sid);
    }
  }

  const payloads = [
    { withdrawPassword: PIN, confirmPassword: PIN },
    { withdrawPass: PIN, confirmWithdrawPass: PIN },
    { password: PIN, confirmPassword: PIN },
    { newWithdrawPassword: PIN, confirmWithdrawPassword: PIN },
    { withdraw_password: PIN, confirm_withdraw_password: PIN },
    { pin: PIN, confirmPin: PIN },
  ];

  const callPost = async (client, path, data) => {
    const full = path.startsWith('http') ? path : (apiBase + path);
    const tries = [
      () => client.post(path, data),
      () => client.post({ url: path, data }),
      () => client.post({ url: full, data }),
      () => client.request({ url: full, method: 'POST', data }),
      () => client.request(path, { method: 'POST', data }),
    ];
    let lastErr = '';
    for (const fn of tries) {
      try {
        const result = await fn();
        return { ok: true, result };
      } catch (e) {
        lastErr = String(e);
      }
    }
    return { ok: false, error: lastErr };
  };

  const looksSuccess = (result) => {
    if (result == null) return false;
    if (typeof result === 'object') {
      const c = result.code ?? result.errorCode ?? result.status;
      if (c === 1 || c === '1' || result.success === true) return true;
      if (c === 0 || c === '0') return false;
    }
    const s = typeof result === 'string' ? result : JSON.stringify(result);
    if (/1134|errorCode/.test(s) && !/"code":1/.test(s)) return false;
    if (/"code":1/.test(s) || /"success":true/.test(s)) return true;
    if (s.length > 40 && /^[A-Za-z0-9+/=_-]+$/.test(s.replace(/\\s/g, '').slice(0, 120))) {
      return true;
    }
    return false;
  };

  const attempts = [];

  const tryPath = async (path) => {
    for (const { label, client } of httpClients) {
      for (const data of payloads) {
        const att = await callPost(client, path, data);
        attempts.push({
          label,
          path,
          dataKeys: Object.keys(data),
          ok: att.ok,
          error: att.error || '',
        });
        if (att.ok && looksSuccess(att.result)) {
          return { ok: true, path, label, data, result: att.result };
        }
      }
    }
    return null;
  };

  let verifyOk = false;
  const verifyHit = await tryPath(verifyPath);
  if (verifyHit) {
    verifyOk = true;
    attempts.push({ step: 'verify', hit: verifyHit.label });
  }

  const modifyHit = await tryPath(modifyPath);
  if (modifyHit) {
    return {
      ok: true,
      apiBase,
      verifyOk,
      modify: modifyHit,
      attempts: attempts.slice(-20),
    };
  }

  if (pinia?._s) {
    for (const [sid, store] of Object.entries(pinia._s)) {
      for (const key of Object.keys(store || {})) {
        if (!/withdraw|Withdraw|security|fundPass|fund_pass/i.test(key)) continue;
        if (typeof store[key] !== 'function') continue;
        for (const data of payloads) {
          try {
            const r = await store[key](data);
            attempts.push({ action: sid + '.' + key, ok: true });
            if (looksSuccess(r)) {
              return {
                ok: true,
                apiBase,
                via: 'pinia_action',
                action: sid + '.' + key,
                result: r,
                attempts: attempts.slice(-20),
              };
            }
          } catch (e) {
            attempts.push({ action: sid + '.' + key, error: String(e) });
          }
        }
      }
    }
  }

  return {
    ok: false,
    error: httpClients.length ? 'api_no_success_response' : 'no_http_client_in_page',
    apiBase,
    httpClients: httpClients.map(c => c.label),
    verifyOk,
    attempts: attempts.slice(-24),
  };
}
"""


def _normalize_pin(pin: str) -> str:
    return re.sub(r"\D", "", str(pin or ""))[:6]


def resolve_hall_api_origin(page, fallback: str = "https://af861c.c168f.com") -> str:
    """Host API hall (vd af861c.c168f.com) từ network đã load."""
    try:
        origin = page.evaluate(
            """() => {
              const urls = performance.getEntriesByType('resource')
                .map(e => e.name)
                .filter(u => u.includes('/hall/api/'));
              for (let i = urls.length - 1; i >= 0; i--) {
                try { return new URL(urls[i]).origin; } catch (e) {}
              }
              return '';
            }"""
        )
        if origin and str(origin).startswith("http"):
            return str(origin).rstrip("/")
    except Exception:
        pass
    fb = (fallback or "").strip().rstrip("/")
    if fb:
        try:
            u = urlparse(fb)
            if u.scheme and u.netloc:
                return f"{u.scheme}://{u.netloc}"
        except Exception:
            pass
    return "https://af861c.c168f.com"


def _response_ok(body: Any, status: int = 200) -> bool:
    if status != 200:
        return False
    if body is None:
        return False
    if isinstance(body, dict):
        code = body.get("code")
        if code == 1 or body.get("success") is True:
            return True
        if code not in (None, 1) and code != "1":
            return False
    raw = str(body).strip()
    if not raw or "1134" in raw:
        return False
    if '"code":1' in raw or '"success":true' in raw:
        return True
    sample = re.sub(r"\s+", "", raw[:200])
    return len(raw) > 32 and bool(re.fullmatch(r"[A-Za-z0-9+/=_-]+", sample))


def set_fund_password_via_browser_api(
    page,
    fund_password: str,
    *,
    api_origin: str = "",
) -> dict[str, Any]:
    """
    Gọi verifyWithdrawPass + modifyWithdrawPass qua HTTP client của Vue app.
    Không click UI — vẫn chạy trong tab đã đăng nhập (session + mã hóa đúng).
    """
    pin = _normalize_pin(fund_password)
    if len(pin) != 6:
        return {"ok": False, "error": "MK rút phải đúng 6 chữ số"}

    origin = api_origin.strip() or resolve_hall_api_origin(page)
    print(
        f"[PROVISION] API browser — origin={origin} (không click UI)…",
        file=sys.stderr,
    )

    try:
        raw = page.evaluate(
            _JS_CALL_WITHDRAW_API,
            {
                "pin": pin,
                "verifyPath": API_VERIFY_WITHDRAW_PASS,
                "modifyPath": API_MODIFY_WITHDRAW,
            },
        )
    except Exception as e:
        return {"ok": False, "error": f"evaluate_failed: {e}", "api_origin": origin}

    if not isinstance(raw, dict):
        return {"ok": False, "error": "bad_evaluate_result", "raw": raw}

    if raw.get("ok"):
        mod = raw.get("modify") or {}
        result = mod.get("result") if isinstance(mod, dict) else raw.get("result")
        print(
            f"[PROVISION] API browser OK — {mod.get('label', raw.get('via', '?'))} "
            f"{mod.get('path', API_MODIFY_WITHDRAW)}",
            file=sys.stderr,
        )
        return {
            "ok": True,
            "via": "browser_http_client",
            "api_origin": raw.get("apiBase") or origin,
            "verify_ok": bool(raw.get("verifyOk")),
            "modify": mod,
            "result_preview": str(result)[:200],
        }

    err = raw.get("error") or "unknown"
    clients = raw.get("httpClients") or []
    print(
        f"[PROVISION] API browser chưa OK: {err} "
        f"(http clients: {len(clients)})",
        file=sys.stderr,
    )
    if clients:
        print(f"  clients: {', '.join(clients[:8])}", file=sys.stderr)
    return {
        "ok": False,
        "error": err,
        "api_origin": raw.get("apiBase") or origin,
        "http_clients": clients,
        "attempts": raw.get("attempts"),
    }
