"""mitmproxy addon: ghi request/response grabred / chat / payment(deposit)."""
from mitmproxy import http
import os

OUT = os.environ.get("MITM_GRABRED_OUT", "")

# Keywords to keep in the focused txt log (full traffic still in .flow).
_KEEP = (
    "grabred",
    "redpacket",
    "/chat/",
    "/payment/",
    "deposit",
    "recharge",
    "payurl",
    "pay_url",
)


def response(flow: http.HTTPFlow) -> None:
    u = flow.request.pretty_url
    low = u.lower()
    if not any(k in low for k in _KEEP):
        return
    body_req = flow.request.get_text(strict=False) or ""
    body_resp = flow.response.get_text(strict=False) if flow.response else ""
    lines = [
        "=" * 60,
        f"{flow.request.method} {u}",
        f"status={flow.response.status_code if flow.response else '?'}",
        "--- request headers ---",
        str(dict(flow.request.headers)),
        "--- request body ---",
        body_req[:8000],
        "--- response body ---",
        body_resp[:8000],
        "",
    ]
    text = "\n".join(lines)
    # Write file first — print can crash on Windows cp1252 (VN unicode in body).
    if OUT:
        with open(OUT, "a", encoding="utf-8") as f:
            f.write(text)
    try:
        print(text)
    except UnicodeEncodeError:
        print(
            f"{flow.request.method} {u} "
            f"status={flow.response.status_code if flow.response else '?'} "
            "(body omitted: console encoding)"
        )
