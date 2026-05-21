# -*- coding: utf-8 -*-
import re
import requests
from pathlib import Path

BASE = "https://c168b2.cc"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"
home = requests.get(BASE + "/", headers={"User-Agent": UA}, timeout=30).text
paths = sorted(set(re.findall(r'(?:src|href)=["\']([^"\']+\.js[^"\']*)["\']', home)))
Path("_js_paths.txt").write_text("\n".join(paths), encoding="utf-8")
print("js count", len(paths))
for p in paths[:30]:
    print(p)
