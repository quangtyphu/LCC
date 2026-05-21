# -*- coding: utf-8 -*-
import re
from pathlib import Path

for name in ("_register_chunk.js", "_common_chunk.js"):
    p = Path(name)
    if not p.exists():
        continue
    t = p.read_text(encoding="utf-8", errors="replace")
    print("===", name, "size", len(t))
    for pat in [
        r"hall/api/[a-zA-Z0-9_/]+",
        r"geetest",
        r"captcha",
        r"register",
        r"sms",
        r"chipher",
        r"member",
    ]:
        found = sorted(set(re.findall(pat, t, re.I)))
        if found:
            print(pat, ":", found[:30])
