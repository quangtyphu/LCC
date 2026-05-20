# -*- coding: utf-8 -*-
"""Đăng ký XOSO66 hàng loạt từ CSV/TXT."""

from __future__ import annotations

import csv
import io
import os
import re
import time
from typing import Any

from xoso66_provision import provision_account

# alias cột (không dấu, lowercase)
_COLUMN_ALIASES: dict[str, str] = {
    "username": "username",
    "user": "username",
    "tai_khoan": "username",
    "taikhoan": "username",
    "password": "password",
    "pass": "password",
    "mk_dang_nhap": "password",
    "phone": "phone",
    "sdt": "phone",
    "so_dien_thoai": "phone",
    "sodienthoai": "phone",
    "account_holder": "account_holder",
    "truename": "account_holder",
    "ten": "account_holder",
    "ho_ten": "account_holder",
    "ten_dk": "account_holder",
    "ten_chu_tk": "account_holder",
    "proxy": "proxy",
    "fund_password": "fund_password",
    "mk_rut": "fund_password",
    "withdraw_pass": "fund_password",
    "bank": "bank",
    "bank_name": "bank",
    "ngan_hang": "bank",
    "account_number": "account_number",
    "stk": "account_number",
    "so_tk": "account_number",
    "card": "account_number",
    "cardnumber": "account_number",
    "device": "device",
    "thiet_bi": "device",
    "vip_level": "vip_level",
    "vip": "vip_level",
}

_POSITIONAL_KEYS = (
    "username",
    "password",
    "phone",
    "account_holder",
    "proxy",
    "fund_password",
    "bank",
    "account_number",
    "device",
)


def _norm_header(h: str) -> str:
    s = (h or "").strip().lower()
    s = re.sub(r"[\s\-]+", "_", s)
    s = re.sub(r"[^\w]", "", s)
    return _COLUMN_ALIASES.get(s, s)


def _detect_delimiter(line: str) -> str:
    if "\t" in line and line.count("\t") >= line.count(","):
        return "\t"
    if ";" in line and line.count(";") > line.count(","):
        return ";"
    return ","


def _looks_like_header(cells: list[str]) -> bool:
    joined = " ".join(c.lower() for c in cells)
    return any(
        k in joined
        for k in ("username", "user", "tai_khoan", "proxy", "phone", "sdt")
    )


def parse_bulk_text(text: str) -> list[dict[str, str]]:
    """Parse CSV/TSV/TXT → list row dict (chưa merge default)."""
    raw = (text or "").lstrip("\ufeff").strip()
    if not raw:
        return []

    lines = [ln for ln in raw.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        return []

    delim = _detect_delimiter(lines[0])
    reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delim)
    rows_raw = list(reader)
    if not rows_raw:
        return []

    out: list[dict[str, str]] = []
    start = 0
    field_keys: list[str] | None = None

    if _looks_like_header(rows_raw[0]):
        field_keys = [_norm_header(c) for c in rows_raw[0]]
        start = 1

    for line_cells in rows_raw[start:]:
        cells = [c.strip() for c in line_cells]
        if not any(cells):
            continue
        row: dict[str, str] = {}
        if field_keys:
            for i, key in enumerate(field_keys):
                if not key or i >= len(cells):
                    continue
                row[key] = cells[i]
        else:
            for i, key in enumerate(_POSITIONAL_KEYS):
                if i < len(cells):
                    row[key] = cells[i]
        if row.get("username"):
            out.append(row)
    return out


def merge_row_defaults(
    row: dict[str, str],
    defaults: dict[str, Any] | None,
) -> dict[str, Any]:
    """Gộp dòng file + default (form / API)."""
    d = dict(defaults or {})
    for k, v in row.items():
        if v is not None and str(v).strip() != "":
            d[k] = str(v).strip()
    d["step"] = "all"
    return d


def validate_bulk_row(body: dict[str, Any], *, index: int) -> str | None:
    u = str(body.get("username") or "").strip()
    if not u:
        return f"dòng {index}: thiếu username"
    if not str(body.get("password") or "").strip():
        return f"dòng {index}: thiếu password"
    if not str(body.get("proxy") or "").strip():
        return f"dòng {index} ({u}): thiếu proxy"
    ph = str(body.get("phone") or "").strip()
    if not ph or len(ph) != 10 or not ph.isdigit():
        return f"dòng {index} ({u}): phone phải 10 chữ số"
    if not str(body.get("account_holder") or "").strip():
        return f"dòng {index} ({u}): thiếu account_holder"
    return None


def provision_bulk(
    rows: list[dict[str, Any]],
    *,
    defaults: dict[str, Any] | None = None,
    stop_on_error: bool = False,
    delay_seconds: float = 3.0,
) -> dict[str, Any]:
    """Đăng ký lần lượt từng dòng. Trả summary + results."""
    results: list[dict[str, Any]] = []
    ok_count = 0
    fail_count = 0
    skip_count = 0

    for i, raw in enumerate(rows, start=1):
        body = merge_row_defaults(raw, defaults)
        err = validate_bulk_row(body, index=i)
        if err:
            results.append(
                {"index": i, "username": body.get("username"), "ok": False, "error": err}
            )
            fail_count += 1
            if stop_on_error:
                break
            continue

        username = body.get("username")
        print(f"[BULK] ({i}/{len(rows)}) {username}…", flush=True)
        try:
            out = provision_account(body)
        except Exception as e:
            out = {"ok": False, "username": username, "error": str(e)}

        skipped = bool(out.get("skipped"))
        item = {
            "index": i,
            "username": username,
            "ok": bool(out.get("ok")),
            "skipped": skipped,
            "account_id": out.get("account_id"),
            "steps": out.get("steps"),
            "error": None
            if skipped or out.get("ok")
            else out.get("error") or _first_fail_msg(out.get("steps") or []),
        }
        results.append(item)
        if skipped:
            skip_count += 1
        elif item["ok"]:
            ok_count += 1
        else:
            fail_count += 1
            if stop_on_error:
                break

        if delay_seconds > 0 and i < len(rows):
            time.sleep(delay_seconds)

    return {
        "ok": fail_count == 0 and (ok_count > 0 or skip_count > 0),
        "total": len(rows),
        "processed": len(results),
        "ok_count": ok_count,
        "skip_count": skip_count,
        "fail_count": fail_count,
        "results": results,
    }


def _first_fail_msg(steps: list[dict]) -> str:
    for s in steps:
        if not s.get("ok") and not s.get("skipped"):
            return str(s.get("msg") or s.get("step") or "thất bại")
    return "provision thất bại"


def main() -> int:
    import argparse
    from pathlib import Path

    ap = argparse.ArgumentParser(description="Đăng ký XOSO66 hàng loạt từ CSV/TXT")
    ap.add_argument("-f", "--file", required=True, help="đường dẫn CSV/TXT")
    ap.add_argument("--password", default=os.environ.get("XOSO66_DEFAULT_LOGIN_PASSWORD", "Valentine1"))
    ap.add_argument("--fund-password", default="270288")
    ap.add_argument("--delay", type=float, default=5.0)
    ap.add_argument(
        "--stop-on-error",
        action="store_true",
        help="dừng hẳn khi gặp lỗi (mặc định: bỏ qua acc lỗi, chạy acc sau)",
    )
    args = ap.parse_args()

    text = Path(args.file).read_text(encoding="utf-8-sig")
    rows = parse_bulk_text(text)
    if not rows:
        print("Không có dòng hợp lệ")
        return 1
    out = provision_bulk(
        rows,
        defaults={"password": args.password, "fund_password": args.fund_password},
        stop_on_error=args.stop_on_error,
        delay_seconds=args.delay,
    )
    print(
        f"Xong: OK {out['ok_count']}, bỏ qua {out.get('skip_count', 0)}, "
        f"lỗi {out['fail_count']} / {out['total']}",
        flush=True,
    )
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
