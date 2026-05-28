# -*- coding: utf-8 -*-
"""Phân tích log hall/api — tách riêng tránh circular import register ↔ manual_capture."""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from typing import Any

BUCKET_RE = [
    ("game", re.compile(r"gameCenter|gameApi", re.I)),
    ("register", re.compile(r"register", re.I)),
    ("login", re.compile(r"login|getFastLogin|signIn", re.I)),
    ("fund_password", re.compile(r"fund|withdraw.*pass|transaction.*pass|security.*pass", re.I)),
    ("bank", re.compile(r"bank|cardbind|card", re.I)),
    ("user_info", re.compile(r"user/info|modifyinfo|vip", re.I)),
    ("finance", re.compile(r"finance|pay|withdraw", re.I)),
]


def _classify(path: str) -> str:
    for name, pat in BUCKET_RE:
        if pat.search(path):
            return name
    return "other"


def _parse_body_preview(body: str) -> dict[str, Any]:
    raw = (body or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            return json.loads(raw.split("\n")[0][:8000])
        except Exception:
            return {}
    if len(raw) > 40 and re.fullmatch(r"[A-Za-z0-9+/=_-]+", raw[:200].replace("\n", "")):
        return {"_encrypted": True, "_len": len(raw)}
    return {"_raw_preview": raw[:200]}


def analyze_api_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_bucket: dict[str, list[str]] = defaultdict(list)
    posts: list[dict[str, Any]] = []

    for ev in events:
        if ev.get("kind") != "response":
            continue
        path = str(ev.get("path") or "")
        if not path:
            continue
        body = str(ev.get("body") or "")
        parsed = _parse_body_preview(body)
        row = {
            "path": path,
            "method": ev.get("method"),
            "status": ev.get("status"),
            "code": parsed.get("code"),
            "msg": (parsed.get("msg") or "")[:120],
            "encrypted": bool(parsed.get("_encrypted")),
        }
        by_path[path].append(row)
        bucket = _classify(path)
        if path not in by_bucket[bucket]:
            by_bucket[bucket].append(path)

        req = next(
            (
                e
                for e in reversed(events)
                if e.get("kind") == "request"
                and e.get("path") == path
                and e.get("method") in ("POST", "PUT", "PATCH")
            ),
            None,
        )
        if req or ev.get("method") in ("POST", "PUT"):
            posts.append(
                {
                    "path": path,
                    "bucket": bucket,
                    "x_data_mode": req.get("x_data_mode") if req else None,
                    "post_preview": (req.get("post_preview") or "")[:500] if req else "",
                    "response_code": parsed.get("code"),
                    "response_msg": (parsed.get("msg") or "")[:200],
                    "encrypted_response": bool(parsed.get("_encrypted")),
                }
            )

    post_paths: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in posts:
        k = p["path"]
        if k not in seen:
            seen.add(k)
            post_paths.append(p)

    return {
        "total_events": len(events),
        "response_count": sum(1 for e in events if e.get("kind") == "response"),
        "unique_paths": sorted(by_path.keys()),
        "by_bucket": {k: sorted(v) for k, v in by_bucket.items()},
        "post_endpoints": post_paths,
        "likely_flow": {
            "register": [x for x in post_paths if x["bucket"] == "register"],
            "login": [x for x in post_paths if x["bucket"] == "login"],
            "fund_password": [x for x in post_paths if x["bucket"] == "fund_password"],
            "bank": [x for x in post_paths if x["bucket"] == "bank"],
        },
    }


def print_analysis_summary(analysis: dict[str, Any]) -> None:
    print("\n=== PHÂN TÍCH API (tóm tắt) ===", file=sys.stderr)
    for bucket, paths in sorted(analysis.get("by_bucket", {}).items()):
        print(f"\n[{bucket}] ({len(paths)} path)", file=sys.stderr)
        for p in paths[:25]:
            print(f"  - {p}", file=sys.stderr)
    bank_flow = analysis.get("likely_flow", {}).get("bank") or []
    if bank_flow:
        print("\n[bank POST trong phiên]", file=sys.stderr)
        for row in bank_flow:
            print(
                f"  - {row.get('path')} code={row.get('response_code')} "
                f"cipher={row.get('encrypted_response')}",
                file=sys.stderr,
            )
