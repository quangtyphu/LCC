# -*- coding: utf-8 -*-
import re
from pathlib import Path

big = Path("_big_line.txt").read_text(encoding="utf-8", errors="replace")
keys = [
    "apiUrl", "api_url", "API_URL", "baseUrl", "baseURL", "gateway",
    "wps", "merchant", "Merchant", "encrypt", "registerUrl",
    "domainUrl", "siteCode", "site_code", "brand", "openApi",
    "pubsg", "cloudfront", "lobbyHost", "requestUrl", "backend",
    "geetest", "captcha", "sms", "member",
]
found = []
for k in keys:
    for m in re.finditer(re.escape(k) + r".{0,120}", big, re.I):
        found.append(m.group(0)[:140])
Path("_scan_keys_out.txt").write_text("\n".join(sorted(set(found))[:300]), encoding="utf-8")
print("found", len(set(found)))
