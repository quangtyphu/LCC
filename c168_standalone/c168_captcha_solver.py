# -*- coding: utf-8 -*-
"""
Giải captcha C168 qua Capsolver / custom.

Capsolver GeeTest: https://docs.capsolver.com/en/guide/captcha/Geetest/
  - V4: task GeeTestTaskProxyLess + captchaId (C168 register dùng id trong config site)
  - V3: thêm gt + challenge (lấy từ init API trên trang, không copy tay từ DevTools)

Capsolver Turnstile: https://docs.capsolver.com/en/guide/captcha/cloudflare_turnstile/
  - AntiTurnstileTaskProxyLess + websiteKey (khi API register trả code 1134)
"""
from __future__ import annotations

import time
from typing import Any

import requests

CAPSOLVER_CREATE = "https://api.capsolver.com/createTask"
CAPSOLVER_RESULT = "https://api.capsolver.com/getTaskResult"


def solve_captcha(cfg: dict[str, Any]) -> dict[str, Any]:
    """Giải captcha theo captcha.kind trong config."""
    cap = cfg.get("captcha") if isinstance(cfg.get("captcha"), dict) else {}
    kind = str(cap.get("kind") or "geetest_v4").strip().lower()
    pageurl = str(cap.get("pageurl") or "").strip()
    if not pageurl:
        pageurl = str(cfg.get("base_url", "")).rstrip("/") + str(
            cfg.get("register_path") or "/home/register"
        )

    if kind in ("geetest_v4", "geetest", "gee_test_v4"):
        return solve_geetest_v4(cfg, pageurl=pageurl)
    if kind in ("geetest_v3", "gee_test_v3"):
        return solve_geetest_v3(cfg, pageurl=pageurl)
    if kind in ("turnstile", "cf_turnstile"):
        return solve_turnstile(cfg, pageurl=pageurl)
    return {"ok": False, "error": f"captcha.kind không hỗ trợ: {kind}"}


def solve_geetest_v4(cfg: dict[str, Any], *, pageurl: str = "") -> dict[str, Any]:
    """
    GeeTest V4 — Capsolver GeeTestTaskProxyLess + captchaId.
    Docs: https://docs.capsolver.com/en/guide/captcha/Geetest/
    """
    cap = cfg.get("captcha") if isinstance(cfg.get("captcha"), dict) else {}
    provider = str(cap.get("provider") or "capsolver").strip().lower()
    captcha_id = str(cap.get("captcha_id") or cap.get("captchaId") or "").strip()
    timeout = int(cap.get("timeout_sec") or 120)
    pageurl = pageurl or str(cap.get("pageurl") or "")

    if not captcha_id:
        return {
            "ok": False,
            "error": "Thiếu captcha.captcha_id (GeeTest V4). Lấy từ geetest_captcha_id.register trên site C168.",
        }

    if provider == "custom":
        return _solve_custom_geetest(cap, captcha_id=captcha_id, pageurl=pageurl, timeout=timeout)
    if provider == "capsolver":
        return _capsolver_geetest_v4(cap, captcha_id=captcha_id, pageurl=pageurl, timeout=timeout)
    return {"ok": False, "error": f"provider geetest_v4: {provider}"}


def solve_geetest_v3(cfg: dict[str, Any], *, pageurl: str = "") -> dict[str, Any]:
    """GeeTest V3 — cần gt + challenge (mới, từ init trên trang)."""
    cap = cfg.get("captcha") if isinstance(cfg.get("captcha"), dict) else {}
    api_key = str(cap.get("api_key") or "").strip()
    gt = str(cap.get("gt") or "").strip()
    challenge = str(cap.get("challenge") or "").strip()
    pageurl = pageurl or str(cap.get("pageurl") or "")
    timeout = int(cap.get("timeout_sec") or 120)

    if not api_key:
        return {"ok": False, "error": "Thiếu captcha.api_key"}
    if not gt or not challenge:
        return {
            "ok": False,
            "error": "GeeTest V3 cần captcha.gt và captcha.challenge (bắt từ request init khi mở captcha, không copy cũ).",
        }

    task = {
        "type": "GeeTestTaskProxyLess",
        "websiteURL": pageurl,
        "gt": gt,
        "challenge": challenge,
    }
    sub = str(cap.get("geetest_api_subdomain") or "").strip()
    if sub:
        task["geetestApiServerSubdomain"] = sub

    res = _capsolver_poll(api_key, task, timeout=timeout)
    if not res.get("ok"):
        return res
    sol = res.get("solution") or {}
    return {
        "ok": True,
        "kind": "geetest_v3",
        "provider": "capsolver",
        "solution": sol,
        "challenge": sol.get("challenge"),
        "validate": sol.get("validate"),
        "seccode": sol.get("seccode"),
    }


def solve_turnstile(cfg: dict[str, Any], *, pageurl: str = "") -> dict[str, Any]:
    cap = cfg.get("captcha") if isinstance(cfg.get("captcha"), dict) else {}
    provider = str(cap.get("provider") or "capsolver").strip().lower()
    sitekey = str(cap.get("sitekey") or cap.get("turnstile_sitekey") or "").strip()
    pageurl = pageurl or str(cap.get("pageurl") or "")
    timeout = int(cap.get("timeout_sec") or 120)
    if not sitekey:
        return {"ok": False, "error": "Thiếu captcha.sitekey (Turnstile)"}

    if provider == "capsolver":
        api_key = str(cap.get("api_key") or "").strip()
        if not api_key:
            return {"ok": False, "error": "Thiếu captcha.api_key"}
        res = _capsolver_poll(
            api_key,
            {
                "type": "AntiTurnstileTaskProxyLess",
                "websiteURL": pageurl,
                "websiteKey": sitekey,
            },
            timeout=timeout,
        )
        if not res.get("ok"):
            return res
        token = str((res.get("solution") or {}).get("token") or "").strip()
        if not token:
            return {"ok": False, "error": "Turnstile: không có token"}
        return {"ok": True, "kind": "turnstile", "token": token, "provider": "capsolver"}
    return {"ok": False, "error": f"turnstile provider: {provider}"}


def _capsolver_geetest_v4(
    cap: dict[str, Any], *, captcha_id: str, pageurl: str, timeout: int
) -> dict[str, Any]:
    api_key = str(cap.get("api_key") or "").strip()
    if not api_key:
        return {"ok": False, "error": "Thiếu captcha.api_key (Capsolver)"}

    task: dict[str, Any] = {
        "type": "GeeTestTaskProxyLess",
        "websiteURL": pageurl,
        "captchaId": captcha_id,
    }
    sub = str(cap.get("geetest_api_subdomain") or "").strip()
    if sub:
        task["geetestApiServerSubdomain"] = sub

    res = _capsolver_poll(api_key, task, timeout=timeout)
    if not res.get("ok"):
        return res
    sol = res.get("solution") or {}
    required = ("captcha_output", "gen_time", "lot_number", "pass_token")
    missing = [k for k in required if not sol.get(k)]
    if missing:
        return {"ok": False, "error": f"Capsolver GeeTest thiếu field: {missing}", "raw": sol}
    return {
        "ok": True,
        "kind": "geetest_v4",
        "provider": "capsolver",
        "solution": sol,
        "captcha_id": sol.get("captcha_id") or captcha_id,
        "captcha_output": sol.get("captcha_output"),
        "gen_time": sol.get("gen_time"),
        "lot_number": sol.get("lot_number"),
        "pass_token": sol.get("pass_token"),
        "risk_type": sol.get("risk_type"),
    }


def _capsolver_poll(api_key: str, task: dict[str, Any], *, timeout: int) -> dict[str, Any]:
    try:
        create = requests.post(
            CAPSOLVER_CREATE,
            json={"clientKey": api_key, "task": task},
            timeout=30,
        ).json()
    except Exception as e:
        return {"ok": False, "error": f"Capsolver createTask: {e}"}

    if create.get("errorId"):
        return {
            "ok": False,
            "error": str(create.get("errorDescription") or create.get("errorCode") or create),
        }
    task_id = create.get("taskId")
    if not task_id:
        return {"ok": False, "error": f"Capsolver không trả taskId: {create}"}

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        try:
            res = requests.post(
                CAPSOLVER_RESULT,
                json={"clientKey": api_key, "taskId": task_id},
                timeout=30,
            ).json()
        except Exception as e:
            return {"ok": False, "error": f"Capsolver getTaskResult: {e}"}

        if res.get("status") == "ready":
            return {"ok": True, "solution": res.get("solution") or {}, "task_id": task_id}
        if res.get("status") == "failed" or res.get("errorId"):
            return {
                "ok": False,
                "error": str(res.get("errorDescription") or res),
                "raw": res,
            }
    return {"ok": False, "error": "Capsolver timeout", "task_id": task_id}


def _solve_custom_geetest(
    cap: dict[str, Any], *, captcha_id: str, pageurl: str, timeout: int
) -> dict[str, Any]:
    url = str(cap.get("custom_url") or "").strip()
    if not url:
        return {"ok": False, "error": "Thiếu captcha.custom_url"}
    payload = {
        "type": "geetest_v4",
        "captcha_id": captcha_id,
        "pageurl": pageurl,
        "action": "register",
    }
    try:
        r = requests.post(url, json=payload, timeout=min(timeout, 180))
        data = r.json() if "json" in (r.headers.get("content-type") or "") else {}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if not isinstance(data, dict):
        return {"ok": False, "error": r.text[:300]}
    if data.get("solution"):
        return {"ok": True, "kind": "geetest_v4", "provider": "custom", "solution": data["solution"]}
    if all(data.get(k) for k in ("captcha_output", "pass_token", "lot_number")):
        return {"ok": True, "kind": "geetest_v4", "provider": "custom", "solution": data}
    return {"ok": False, "error": str(data.get("error") or data)}


def inject_geetest_solution(page, solved: dict[str, Any]) -> bool:
    """Đưa solution GeeTest V3/V4 vào Vue/store trước khi bấm Đăng ký."""
    if solved.get("kind") == "geetest_v3":
        payload = {
            "challenge": solved.get("challenge"),
            "validate": solved.get("validate"),
            "seccode": solved.get("seccode"),
        }
    else:
        sol = solved.get("solution") if isinstance(solved.get("solution"), dict) else solved
        payload = {
            "captcha_id": solved.get("captcha_id") or sol.get("captcha_id"),
            "captcha_output": solved.get("captcha_output") or sol.get("captcha_output"),
            "gen_time": solved.get("gen_time") or sol.get("gen_time"),
            "lot_number": solved.get("lot_number") or sol.get("lot_number"),
            "pass_token": solved.get("pass_token") or sol.get("pass_token"),
            "risk_type": solved.get("risk_type") or sol.get("risk_type"),
        }
    return bool(
        page.evaluate(
            """(p) => {
              let ok = false;
              try {
                const app = document.querySelector('#app')?.__vue_app__;
                const gp = app?.config?.globalProperties;
                if (gp?.$pinia) {
                  for (const store of Object.values(gp.$pinia._s || {})) {
                    if (!store) continue;
                    const targets = [
                      store.registerForm, store.loginRegisterForm, store.form,
                      store.register, store
                    ].filter(Boolean);
                    for (const t of targets) {
                      if (typeof t !== 'object') continue;
                      Object.assign(t, p);
                      if ('geetest' in t) t.geetest = { ...t.geetest, ...p };
                      if ('captcha' in t) t.captcha = p;
                      ok = true;
                    }
                  }
                }
              } catch (e) {}
              window.__c168_geetest_solution = p;
              return ok || !!window.__c168_geetest_solution;
            }""",
            payload,
        )
    )


def inject_turnstile_token(page, token: str) -> dict[str, Any]:
    """Đưa token Turnstile vào DOM + Pinia (site mã hóa body, cần token trong store)."""
    raw = page.evaluate(
        """(token) => {
          const methods = [];
          const setVal = (el) => {
            if (!el) return false;
            el.value = token;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            return true;
          };
          for (const sel of [
            'textarea[name="cf-turnstile-response"]',
            'input[name="cf-turnstile-response"]',
            '[name="cf-turnstile-response"]',
          ]) {
            if (setVal(document.querySelector(sel))) methods.push('dom:' + sel);
          }
          let el = document.querySelector('[name="cf-turnstile-response"]');
          if (!el) {
            el = document.createElement('textarea');
            el.name = 'cf-turnstile-response';
            el.style.cssText = 'display:none';
            document.body.appendChild(el);
            setVal(el);
            methods.push('created_hidden_input');
          }
          window.cf_turnstile_response = token;
          window.__c168_turnstile_token = token;
          methods.push('window_global');

          const KEY_HINTS = ['turnstile', 'captcha', 'cftoken', 'cf_token', 'verify', 'cloudflare'];
          const walk = (obj, path, depth) => {
            if (!obj || typeof obj !== 'object' || depth > 5) return;
            for (const [k, v] of Object.entries(obj)) {
              const lk = k.toLowerCase();
              const p = path ? path + '.' + k : k;
              if (typeof v === 'string' && KEY_HINTS.some(h => lk.includes(h))) {
                obj[k] = token;
                methods.push('store:' + p);
              }
              if (v && typeof v === 'object' && !Array.isArray(v)) walk(v, p, depth + 1);
            }
          };
          try {
            const app = document.querySelector('#app')?.__vue_app__;
            const pinia = app?.config?.globalProperties?.$pinia;
            if (pinia?._s) {
              for (const [sid, store] of Object.entries(pinia._s)) {
                if (store && typeof store === 'object') walk(store, 'pinia.' + sid, 0);
              }
            }
          } catch (e) {}

          if (window.turnstile?.setResponse) {
            try { window.turnstile.setResponse(token); methods.push('turnstile.setResponse'); } catch (e) {}
          }
          return { ok: methods.length > 0, methods };
        }""",
        token,
    )
    if isinstance(raw, dict):
        return {"ok": bool(raw.get("ok")), "methods": raw.get("methods") or []}
    return {"ok": bool(raw), "methods": []}
