# -*- coding: utf-8 -*-
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from xoso66_paths import apply_default_env

apply_default_env()

from xoso66_cms_chrome import resolve_cms_chrome_by_device
from xoso66_chrome_profile import (
    _chrome_aes_key,
    _copy_cookies_db_for_read,
    _cookies_db_path,
    _decrypt_cookie_value,
    profile_has_cf_clearance,
    profile_is_locked,
    read_profile_cookies,
    warm_session_from_profile,
)
from xoso66_session import BASE_URL, sync_session_from_chrome
from urllib.parse import urlparse


def main() -> None:
    cms = resolve_cms_chrome_by_device("XMSB76")
    print("cms:", cms)
    p = Path(cms["profile_dir"])
    host = urlparse(BASE_URL).netloc
    print("BASE host:", host)
    print("profile locked:", profile_is_locked(p))
    print("has_cf_clearance:", profile_has_cf_clearance(p, host=host))
    print("read_profile_cookies:", sorted(read_profile_cookies(p, host=host).keys()))

    db = _cookies_db_path(p)
    tmp = _copy_cookies_db_for_read(db)
    print("cookie db:", db, "copy:", tmp)
    if not tmp:
        return
    key = _chrome_aes_key(p)
    conn = sqlite3.connect(str(tmp))
    rows = conn.execute(
        "SELECT name, host_key, value, encrypted_value FROM cookies ORDER BY host_key, name"
    ).fetchall()
    print("total cookies:", len(rows))
    for name, hk, val, enc in rows:
        dec = str(val or "").strip()
        if not dec and enc:
            dec = _decrypt_cookie_value(bytes(enc), key)[:20] + "…"
        mark = " ***" if name == "cf_clearance" else ""
        print(f"  {name:25} {hk:35} {dec[:30]}{mark}")

    session = {"proxy": cms.get("proxy"), "cookies": {}, "headers": {}}
    warm = warm_session_from_profile(session, p, host=host)
    print("warm:", warm)

    print("\n--- sync test ---")
    out = sync_session_from_chrome(
        "acc17840", device="XMSB76", force_login=False, timeout_sec=20
    )
    print("sync ok:", out.get("ok"), "error:", out.get("error"))
    print("msg:", out.get("msg"))


if __name__ == "__main__":
    main()
