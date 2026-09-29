(function () {
  const PREFIX = "xoso66_login=";
  const STORE_KEY = "xoso66_login_payload_v1";
  const RUN_KEY = "xoso66_login_running_v1";

  let callback = "";
  let captchaPoll = "";
  let finished = false;

  function report(obj) {
    if (!callback) {
      console.log("[xoso66_login]", obj);
      return;
    }
    try {
      chrome.runtime.sendMessage({
        type: "xoso66_login_result",
        callback: callback,
        data: obj,
      });
    } catch (e) {
      console.log("[xoso66_login] sendMessage failed", e, obj);
    }
  }

  function bgFetch(url) {
    return new Promise(function (resolve) {
      try {
        chrome.runtime.sendMessage({ type: "xoso66_login_fetch", url: url }, function (resp) {
          resolve(resp || { ok: false, error: "no_response" });
        });
      } catch (e) {
        resolve({ ok: false, error: String(e) });
      }
    });
  }

  function sleep(ms) {
    return new Promise(function (r) {
      setTimeout(r, ms);
    });
  }

  function readPayloadFromHashQuery() {
    const hash = String(location.hash || "");
    const q = hash.indexOf("?");
    if (q < 0) return null;
    const params = new URLSearchParams(hash.slice(q + 1));
    const v = params.get("xoso66_login");
    if (!v) return null;
    return JSON.parse(atob(v));
  }

  function readPayloadFromQuery() {
    const params = new URLSearchParams(location.search);
    const fromQuery = params.get("xoso66_login");
    if (!fromQuery) return null;
    return JSON.parse(atob(fromQuery));
  }

  function readPayloadFromHash() {
    const hash = String(location.hash || "");
    if (hash.indexOf(PREFIX) < 0) return null;
    const idx = hash.indexOf(PREFIX);
    let raw = hash.slice(idx + PREFIX.length);
    const amp = raw.indexOf("&");
    if (amp >= 0) raw = raw.slice(0, amp);
    return JSON.parse(atob(raw));
  }

  let payload = null;
  try {
    payload =
      readPayloadFromHashQuery() ||
      readPayloadFromQuery() ||
      readPayloadFromHash();
    if (!payload) {
      const cached = sessionStorage.getItem(STORE_KEY);
      if (cached) payload = JSON.parse(cached);
    }
    if (!payload) return;
    sessionStorage.setItem(STORE_KEY, JSON.stringify(payload));
  } catch (e) {
    return;
  }

  // Tránh chạy 2 vòng song song trên cùng tab.
  try {
    if (sessionStorage.getItem(RUN_KEY) === "1") return;
    sessionStorage.setItem(RUN_KEY, "1");
  } catch (e) {}

  const badRoute =
    /^#\/xoso66_login=/.test(location.hash || "") ||
    /^#xoso66_login=/.test(location.hash || "");
  if (badRoute) {
    try {
      history.replaceState(null, "", location.pathname + location.search + "#/");
    } catch (e) {
      location.hash = "#/";
    }
  }

  callback = String(payload._callback || "");
  captchaPoll = String(payload._captcha_poll || "");
  const username = String(payload.username || "");
  const password = String(payload.password || "");

  report({ phase: "started", href: String(location.href || "").slice(0, 120) });

  function onCfVerify() {
    return String(location.href || "").indexOf("/__verify/check") >= 0;
  }

  function getVm() {
    const app = document.querySelector("#app");
    return app && app.__vue__ ? app.__vue__ : null;
  }

  function isLoggedIn(vm) {
    try {
      if (vm && vm.$store && vm.$store.state && vm.$store.state.user) {
        const u = vm.$store.state.user;
        if (u.ukey || u.token || (u.info && (u.info.ukey || u.info.username))) {
          return true;
        }
        if (u.isLogin === true || u.login === true || u.loggedIn === true) {
          return true;
        }
      }
    } catch (e) {}
    // Header không còn ô password = đã login.
    const pass = document.querySelector('input[type="password"]');
    if (!pass || pass.offsetParent === null) {
      const bodyTxt = String((document.body && document.body.innerText) || "");
      if (username && bodyTxt.indexOf(username) >= 0) return true;
      if (/số dư|thành viên|đăng xuất|ký quỹ/i.test(bodyTxt)) return true;
    }
    return false;
  }

  function setInputValue(el, value) {
    if (!el) return;
    el.focus();
    const proto = window.HTMLInputElement && window.HTMLInputElement.prototype;
    const desc = proto && Object.getOwnPropertyDescriptor(proto, "value");
    if (desc && desc.set) desc.set.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new InputEvent("input", { bubbles: true, data: value }));
  }

  function findLoginControls() {
    const inputs = Array.prototype.slice.call(document.querySelectorAll("input") || []);
    let userEl = null;
    let passEl = null;
    let capEl = null;
    for (let i = 0; i < inputs.length; i++) {
      const el = inputs[i];
      if (el.offsetParent === null && el.type !== "hidden") continue;
      const t = String(el.type || "").toLowerCase();
      const n = String(el.name || el.id || el.placeholder || "").toLowerCase();
      if (!passEl && t === "password") passEl = el;
      else if (!userEl && (t === "text" || t === "tel" || t === "email" || t === "")) {
        userEl = el;
      }
      if (!capEl && (t === "text" || t === "") && /captcha|xác nhận|mã/.test(n)) {
        capEl = el;
      }
    }
    // Ô captcha thường nằm sau password.
    if (!capEl && passEl) {
      const all = inputs.filter(function (el) {
        return el !== userEl && el !== passEl && el.type !== "password" && el.offsetParent !== null;
      });
      if (all.length === 1) capEl = all[0];
    }
    return { userEl: userEl, passEl: passEl, capEl: capEl };
  }

  function fillLoginForm(cap) {
    const c = findLoginControls();
    setInputValue(c.userEl, username);
    setInputValue(c.passEl, password);
    if (c.capEl && cap) setInputValue(c.capEl, String(cap));
    return c;
  }

  function clickLoginButton() {
    const nodes = Array.prototype.slice.call(
      document.querySelectorAll("button, a, input[type='submit'], div, span")
    );
    let best = null;
    for (let i = 0; i < nodes.length; i++) {
      const el = nodes[i];
      if (el.offsetParent === null) continue;
      const txt = String(el.textContent || el.value || "")
        .replace(/\s+/g, " ")
        .trim();
      if (txt !== "Đăng nhập") continue;
      // Ưu tiên button/a gần form login.
      const tag = String(el.tagName || "").toLowerCase();
      if (tag === "button" || tag === "a" || tag === "input") {
        best = el;
        break;
      }
      if (!best) best = el;
    }
    if (!best) return false;
    try {
      best.click();
      return true;
    } catch (e) {
      try {
        best.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
        return true;
      } catch (err) {
        return false;
      }
    }
  }

  function needsCaptcha(r) {
    if (!r || typeof r !== "object") return false;
    const code = Number(r.code);
    const msg = String(r.msg || "");
    if (code === 1011) return true;
    return /mã xác nhận|captcha|xác nhận không chính xác/i.test(msg);
  }

  function loginOk(r) {
    return r && typeof r === "object" && Number(r.code) === 1;
  }

  async function waitCaptchaText(timeoutMs) {
    if (!captchaPoll) return "";
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const resp = await bgFetch(captchaPoll);
      if (resp && resp.ok && resp.data) {
        const t = String(resp.data.text || "").trim();
        if (t) return t;
        if (resp.data.error) throw new Error(String(resp.data.error));
      }
      await sleep(800);
    }
    return "";
  }

  async function dispatchLogin(vm, cap) {
    try {
      return await vm.$store.dispatch("user/login", {
        username: username,
        password: password,
        captcha: String(cap || ""),
      });
    } catch (e) {
      let detail = "";
      try {
        detail = typeof e === "object" && e !== null ? JSON.stringify(e) : String(e);
      } catch (err) {
        detail = String(e);
      }
      return {
        _dispatch_error: true,
        code: 0,
        msg: e && e.message ? String(e.message) : detail,
        error: detail,
      };
    }
  }

  function finishOk(extra) {
    if (finished) return;
    finished = true;
    try {
      sessionStorage.removeItem(STORE_KEY);
      sessionStorage.removeItem(RUN_KEY);
    } catch (e) {}
    report(Object.assign({ ok: true }, extra || {}));
  }

  function finishFail(extra) {
    if (finished) return;
    finished = true;
    try {
      sessionStorage.removeItem(RUN_KEY);
    } catch (e) {}
    report(Object.assign({ ok: false }, extra || {}));
  }

  (async function main() {
    const deadline = Date.now() + 120000;
    let captcha = String(payload.captcha || "");
    let lastResp = null;
    let clicks = 0;

    while (Date.now() < deadline && !finished) {
      if (onCfVerify()) {
        report({ phase: "cf_verify", msg: "Chờ vượt Cloudflare (chọn con vật nếu có)" });
        await sleep(1500);
        continue;
      }

      const vm = getVm();
      if (vm && isLoggedIn(vm)) {
        finishOk({ response: { code: 1, msg: "already_logged_in" }, via: "store" });
        return;
      }

      if (!vm || !vm.$store) {
        report({ phase: "wait_vue" });
        await sleep(800);
        continue;
      }

      const controls = fillLoginForm(captcha);
      if (!controls.passEl) {
        report({ phase: "wait_form" });
        await sleep(800);
        continue;
      }

      // 1) Thử Vue dispatch (nền)
      lastResp = await dispatchLogin(vm, captcha);
      if (loginOk(lastResp)) {
        finishOk({ response: lastResp, via: "vue_dispatch" });
        return;
      }

      // 2) Bấm Đăng nhập đúng như tay — đây là path chính
      const clicked = clickLoginButton();
      clicks += clicked ? 1 : 0;
      report({
        phase: "clicked_login",
        clicks: clicks,
        clicked: clicked,
        has_user: !!controls.userEl,
        has_pass: !!controls.passEl,
      });
      await sleep(2500);

      if (isLoggedIn(vm) || isLoggedIn(getVm())) {
        finishOk({ response: lastResp || { code: 1 }, via: "click", clicks: clicks });
        return;
      }

      if (needsCaptcha(lastResp)) {
        report({
          ok: false,
          need_captcha: true,
          attempt: clicks,
          response: lastResp,
        });
        try {
          const text = await waitCaptchaText(90000);
          if (!text) {
            finishFail({ error: "captcha_timeout", response: lastResp });
            return;
          }
          captcha = text;
          continue;
        } catch (e) {
          finishFail({ error: String(e && e.message ? e.message : e), response: lastResp });
          return;
        }
      }

      // Captcha hiện trên UI (chưa có trong response)
      const again = findLoginControls();
      if (again.capEl && !captcha) {
        report({ phase: "captcha_field_visible" });
      }

      await sleep(1200);
    }

    if (!finished) {
      finishFail({
        error: "login_timeout",
        clicks: clicks,
        response: lastResp,
        href: String(location.href || "").slice(0, 160),
      });
    }
  })();
})();
