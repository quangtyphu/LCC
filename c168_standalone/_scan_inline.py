# -*- coding: utf-8 -*-
import re
from pathlib import Path

lines = Path("_probe_home.txt").read_text(encoding="utf-8", errors="replace").splitlines()
big = max(lines, key=len)
Path("_big_line.txt").write_text(big[:500000], encoding="utf-8")
patterns = [
    r"https?://[a-z0-9.-]+\.[a-z]{2,}(?:/[a-zA-Z0-9._/-]*)?",
    r"api[A-Za-z_]*[\"']?\s*[:=]\s*[\"']([^\"']+)",
    r"host[\"']?\s*[:=]\s*[\"']([^\"']+)",
    r"merchant[\"']?\s*[:=]\s*[\"']([a-z0-9]+)",
    r"Merchant[\"']?\s*:\s*[\"']([a-z0-9]+)",
    r"baseURL[\"']?\s*:\s*[\"']([^\"']+)",
    r"gatewayUrl[\"']?\s*:\s*[\"']([^\"']+)",
    r"open8808",
    r"op88",
    r"vndk",
    r"wps",
]
out = []
for pat in patterns:
    for m in re.finditer(pat, big, re.I):
        s = m.group(0) if m.lastindex is None else m.group(1)
        if len(s) < 200:
            out.append(f"{pat}: {s}")
seen = set()
uniq = []
for x in out:
    if x not in seen:
        seen.add(x)
        uniq.append(x)
Path("_scan_inline_out.txt").write_text("\n".join(uniq[:200]), encoding="utf-8")
print("matches", len(uniq))
