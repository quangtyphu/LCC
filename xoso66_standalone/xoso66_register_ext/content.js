(function () {
  const PREFIX = "xoso66_reg=";
  let callback = "";

  function report(obj) {
    if (!callback) {
      console.log("[xoso66_reg]", obj);
      return;
    }
    try {
      chrome.runtime.sendMessage({
        type: "xoso66_reg_result",
        callback: callback,
        data: obj,
      });
    } catch (e) {
      console.log("[xoso66_reg] sendMessage failed", e, obj);
    }
  }

  let payload;
  try {
    const params = new URLSearchParams(location.search);
    const fromQuery = params.get("xoso66_reg");
    if (fromQuery) {
      payload = JSON.parse(atob(fromQuery));
    } else if (location.hash && location.hash.indexOf(PREFIX) >= 0) {
      const idx = location.hash.indexOf(PREFIX);
      payload = JSON.parse(atob(location.hash.slice(idx + PREFIX.length)));
    } else {
      report({ ok: false, error: "missing_payload" });
      return;
    }
  } catch (e) {
    report({ ok: false, error: "bad_payload", detail: String(e) });
    return;
  }

  callback = String(payload._callback || "");
  const plain = payload.body || payload;
  delete plain._callback;

  function onCfVerify() {
    return String(location.href || "").indexOf("/__verify/check") >= 0;
  }

  function waitVue(ms) {
    const deadline = Date.now() + ms;
    return new Promise(function (resolve) {
      (function tick() {
        if (onCfVerify()) {
          resolve(null);
          return;
        }
        const app = document.querySelector("#app");
        const vm = app && app.__vue__;
        if (vm && vm.$store) {
          resolve(vm);
          return;
        }
        if (Date.now() >= deadline) {
          resolve(null);
          return;
        }
        setTimeout(tick, 500);
      })();
    });
  }

  (async function () {
    const vm = await waitVue(45000);
    if (!vm) {
      report({
        ok: false,
        error: onCfVerify() ? "cf_verify_blocked" : "no_vue_store",
      });
      return;
    }
    try {
      const r = await vm.$store.dispatch("user/register", plain);
      report({ ok: true, response: r });
    } catch (e) {
      let detail = "";
      try {
        detail = typeof e === "object" && e !== null ? JSON.stringify(e) : String(e);
      } catch (err) {
        detail = String(e);
      }
      report({
        ok: false,
        error: detail,
        message: e && e.message ? String(e.message) : detail,
      });
    }
  })();
})();
