# -*- coding: utf-8 -*-
"""Chạy thử đăng ký C168 với nhiều cấu hình — in bảng kết quả."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from c168_config_util import load_config
from c168_register import (
    random_password,
    random_phone_vn,
    random_realname,
    random_username,
    register_account,
)

OUT = Path(__file__).resolve().parent / "_matrix_results.json"


def _summary(out: dict) -> str:
    if out.get("ok"):
        return "OK đăng ký"
    err = str(out.get("error") or "")[:80]
    if "robot" in err.lower() or "mẹo" in err.lower() or "chặn bot" in err.lower():
        return "chặn bot"
    if out.get("code") == 1134 or "1134" in str(out.get("register_response") or ""):
        return "captcha 1134"
    if "Turnstile" in err:
        return "turnstile/API"
    return err or "?"


def run_case(name: str, *, cfg: dict, **kwargs) -> dict:
    t0 = time.time()
    out = register_account(
        username=random_username(),
        password=random_password(),
        phone=random_phone_vn(),
        realname=random_realname(),
        cfg=cfg,
        **kwargs,
    )
    elapsed = round(time.time() - t0, 1)
    row = {
        "case": name,
        "ok": bool(out.get("ok")),
        "summary": _summary(out),
        "proxy": out.get("proxy") or "",
        "captcha_mode": out.get("captcha_mode", ""),
        "turnstile_has_response": (out.get("turnstile_wait") or {}).get("has_response"),
        "browser_mode_failed": out.get("browser_mode_failed"),
        "elapsed_sec": elapsed,
        "error": (out.get("error") or "")[:200],
        "register_code": None,
    }
    try:
        body = json.loads(out.get("register_response") or "{}")
        row["register_code"] = body.get("code")
    except Exception:
        pass
    print(f"[{name}] ok={row['ok']} {row['summary']} ({elapsed}s) proxy={row['proxy'][:50]}", flush=True)
    return row


def main() -> int:
    base = load_config()
    cases: list[tuple[str, dict, dict]] = [
        (
            "A_browser+proxy+headed",
            {**base, "playwright": {**base.get("playwright", {}), "headless": False}},
            {"use_proxy_db": True, "captcha_mode": ""},
        ),
        (
            "B_browser+proxy+headless",
            {**base, "playwright": {**base.get("playwright", {}), "headless": True}},
            {"use_proxy_db": True, "captcha_mode": ""},
        ),
        (
            "C_browser+no_proxy+headed",
            {**base, "playwright": {**base.get("playwright", {}), "headless": False}},
            {"use_proxy_db": False, "captcha_mode": ""},
        ),
        (
            "D_capsolver+proxy+headed",
            {**base, "playwright": {**base.get("playwright", {}), "headless": False}},
            {"use_proxy_db": True, "captcha_mode": "capsolver"},
        ),
        (
            "E_browser+proxy+headed_slow",
            {
                **base,
                "playwright": {
                    **base.get("playwright", {}),
                    "headless": False,
                    "human_delay_ms": 200,
                    "pause_between_fields_ms": 900,
                    "pause_before_submit_ms": 4000,
                },
                "captcha": {**base.get("captcha", {}), "wait_after_submit_ms": 120_000},
            },
            {"use_proxy_db": True, "captcha_mode": ""},
        ),
    ]

    rows: list[dict] = []
    for name, cfg, kw in cases:
        try:
            rows.append(run_case(name, cfg=cfg, **kw))
        except Exception as e:
            rows.append({"case": name, "ok": False, "summary": f"exception: {e}"})
        time.sleep(3)

    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== BẢNG KẾT QUẢ ===")
    for r in rows:
        print(
            f"{r['case']:30} ok={str(r.get('ok')):5} "
            f"code={r.get('register_code')} ts={r.get('turnstile_has_response')} "
            f"{r.get('summary')}"
        )
    print(f"\nChi tiết: {OUT}")
    return 0 if any(r.get("ok") for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
