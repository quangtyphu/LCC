chrome.runtime.onMessage.addListener((msg, _sender, _sendResponse) => {
  if (!msg || !msg.type) {
    return;
  }
  if (msg.type === "xoso66_login_result") {
    const callback = String(msg.callback || "");
    const data = msg.data || {};
    if (!callback) {
      return;
    }
    fetch(callback, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
      keepalive: true,
    }).catch(() => {});
    return;
  }
  if (msg.type === "xoso66_login_fetch") {
    const url = String(msg.url || "");
    if (!url) {
      _sendResponse({ ok: false, error: "missing_url" });
      return true;
    }
    fetch(url, { method: "GET", cache: "no-store" })
      .then((r) => r.json())
      .then((j) => _sendResponse({ ok: true, data: j }))
      .catch((e) => _sendResponse({ ok: false, error: String(e) }));
    return true;
  }
});
