#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gọi API refresh-balance cho mọi acc trong DB (site → cập nhật DB)."""
from __future__ import annotations

import concurrent.futures
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8799"
API_KEY = "doi-api-key-cms"
WORKERS = 12
TIMEOUT = 120


def api(method: str, path: str, body: dict | None = None) -> dict | list:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def refresh_one(item: tuple[str, str]) -> tuple[bool, str, float | None, str | None]:
    aid, user = item
    try:
        out = api("POST", f"/api/accounts/{aid}/refresh-balance")
        bal = float(out.get("balance") or 0)
        print(f"  OK {user}: {bal:,.0f}", flush=True)
        return True, user, bal, None
    except urllib.error.HTTPError as e:
        err = e.read().decode(errors="replace")[:300]
        print(f"  FAIL {user}: HTTP {e.code} {err}", flush=True)
        return False, user, None, err
    except Exception as e:
        print(f"  FAIL {user}: {e}", flush=True)
        return False, user, None, str(e)


def main() -> int:
    accs = api("GET", "/api/accounts")
    if not isinstance(accs, list):
        print("API /api/accounts không trả list", flush=True)
        return 1
    items = [
        (str(a.get("id") or "").strip(), str(a.get("username") or a.get("id") or ""))
        for a in accs
    ]
    items = [x for x in items if x[0]]
    print(
        f"[BALANCE-API] {len(items)} acc — POST /api/accounts/{{id}}/refresh-balance, "
        f"{WORKERS} luồng",
        flush=True,
    )
    results: list[tuple[bool, str, float | None, str | None]] = []
    ok = fail = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for r in ex.map(refresh_one, items):
            results.append(r)
            if r[0]:
                ok += 1
            else:
                fail += 1

    results.sort(key=lambda x: (-(float(x[2] or 0)), str(x[1])))
    print(f"\n[BALANCE-API] xong: {ok} OK, {fail} lỗi / {len(items)} acc", flush=True)
    print("--- Top 20 số dư ---", flush=True)
    shown = 0
    for r in results:
        if r[0]:
            print(f"  {r[1]}: {float(r[2]):,.0f}", flush=True)
            shown += 1
            if shown >= 20:
                break
    if fail:
        print("--- Lỗi ---", flush=True)
        for r in results:
            if not r[0]:
                print(f"  {r[1]}: {r[3]}", flush=True)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
