chrome.runtime.onMessage.addListener((msg, _sender, _sendResponse) => {
  if (!msg || msg.type !== "xoso66_reg_result") {
    return;
  }
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
});
