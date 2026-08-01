"""
Mỗi CHECK_INTERVAL_SECONDS (mặc định 3600s): cộng tổng số dư tất cả device Banking DB.
Nếu tổng > TOTAL_BALANCE_THRESHOLD_VND → gửi 1 lệnh rút qua Banking API.
"""

from __future__ import annotations

import os
import random
import string
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from constants import REPO_ROOT, load_config

_LOG = "[DEVICE-BALANCE-OVERFLOW]"
_tick_lock = threading.Lock()
_NDCK_LENGTH = 10
_cached_internal_token: str | None = None


def _cfg() -> dict[str, Any]:
    raw = load_config() or {}
    block = raw.get("DEVICE_BALANCE_OVERFLOW_DEPOSIT")
    if not isinstance(block, dict):
        block = {}
    dep = raw.get("LC79_DEPOSIT") if isinstance(raw.get("LC79_DEPOSIT"), dict) else {}
    third = str(dep.get("third_party_url") or "").strip()
    banking_from_deposit = third.rsplit("/api/", 1)[0].rstrip("/") if third else ""
    return {
        "ENABLED": int(block.get("ENABLED", 0) or 0),
        "CHECK_INTERVAL_SECONDS": int(block.get("CHECK_INTERVAL_SECONDS", 3600) or 3600),
        "TOTAL_BALANCE_THRESHOLD_VND": int(
            block.get("TOTAL_BALANCE_THRESHOLD_VND", 150_000_000) or 150_000_000
        ),
        "DEPOSIT_AMOUNT_VND": int(block.get("DEPOSIT_AMOUNT_VND", 1_000_000) or 1_000_000),
        "BANK": str(block.get("BANK") or "VIB").strip(),
        "ACCOUNT_NUMBER": str(block.get("ACCOUNT_NUMBER") or "092092078  "),
        "ACCOUNT_HOLDER": str(block.get("ACCOUNT_HOLDER") or "NGUYEN VAN QUANG").strip(),
        "BANKING_API_URL": str(
            block.get("BANKING_API_URL") or banking_from_deposit or "http://127.0.0.1:3010"
        ).rstrip("/"),
        "INTERNAL_DB_TOKEN": str(block.get("INTERNAL_DB_TOKEN") or "").strip(),
    }


def _random_ndck(length: int = _NDCK_LENGTH) -> str:
    return "".join(random.choices(string.ascii_uppercase, k=length))


def _token_from_banking_env() -> str:
    """Đọc INTERNAL_DB_TOKEN từ Banking/.env (sibling của LC79)."""
    env_path = REPO_ROOT.parent / "Banking" / ".env"
    try:
        text = Path(env_path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, _, val = s.partition("=")
        if key.strip() != "INTERNAL_DB_TOKEN":
            continue
        return val.strip().strip('"').strip("'")
    return ""


def _internal_db_token(cfg_token: str = "") -> str:
    global _cached_internal_token
    if _cached_internal_token is not None:
        return _cached_internal_token
    token = (
        str(os.getenv("INTERNAL_DB_TOKEN") or "").strip()
        or str(cfg_token or "").strip()
        or _token_from_banking_env()
    )
    _cached_internal_token = token
    return token


def _banking_headers(cfg_token: str = "") -> dict[str, str]:
    token = _internal_db_token(cfg_token)
    if not token:
        return {}
    return {"X-Internal-Token": token}


def fetch_all_device_balances(banking_url: str, *, cfg_token: str = "") -> dict[str, int]:
    """Lấy số dư tất cả device từ Banking DB (GET /api/device-balances)."""
    base = banking_url.rstrip("/")
    url = f"{base}/api/device-balances"
    try:
        r = requests.get(url, headers=_banking_headers(cfg_token), timeout=15)
        if r.status_code == 200:
            payload = r.json()
            rows = payload.get("items") if isinstance(payload, dict) else payload
            if isinstance(rows, list):
                result: dict[str, int] = {}
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    device = str(row.get("device") or "").strip()
                    if not device:
                        continue
                    try:
                        result[device] = int(row.get("balance") or 0)
                    except (TypeError, ValueError):
                        result[device] = 0
                if result:
                    return result
            print(f"{_LOG} ⚠️ Banking {url}: response trống hoặc sai format", flush=True)
        else:
            print(
                f"{_LOG} ⚠️ Banking {url}: HTTP {r.status_code} {(r.text or '')[:200]}",
                flush=True,
            )
    except Exception as ex:
        print(f"{_LOG} ⚠️ Banking API {url}: {ex}", flush=True)
    return {}


def sum_balances(balances: dict[str, int]) -> int:
    return sum(int(v or 0) for v in balances.values())


# Prefix orderId — callback handler nhận diện lệnh gom tiền, bỏ poll lịch sử user.
OVERFLOW_ORDER_PREFIX = "overflow_"


def build_withdraw_order(cfg: dict[str, Any]) -> dict[str, Any]:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    holder = cfg["ACCOUNT_HOLDER"]
    return {
        "orderId": f"{OVERFLOW_ORDER_PREFIX}{ts}_001",
        "bank": cfg["BANK"],
        "beneficiaryBank": cfg["BANK"],
        "account_number": cfg["ACCOUNT_NUMBER"],
        "accountHolder": holder,
        # Không phải user game — chỉ để banking hiển thị; không verify/poll lịch sử.
        "username": holder,
        "amount": cfg["DEPOSIT_AMOUNT_VND"],
        "transferContent": _random_ndck(),
        "skipCallback": True,
        "skip_callback": True,
        "skipVerify": True,
        "partnerId": "AZP",
        "purpose": "device_balance_overflow",
    }


def send_withdraw_order(cfg: dict[str, Any], data: dict[str, Any]) -> bool:
    url = f"{cfg['BANKING_API_URL']}/api/orders/withdraw"
    try:
        r = requests.post(
            url,
            json=data,
            headers=_banking_headers(cfg.get("INTERNAL_DB_TOKEN") or ""),
            timeout=15,
        )
        if r.status_code == 200:
            return True
        print(
            f"{_LOG} ❌ Banking {r.status_code}: {(r.text or '')[:200]}",
            flush=True,
        )
        return False
    except requests.exceptions.ConnectionError:
        print(f"{_LOG} ❌ Không kết nối Banking tại {url}", flush=True)
        return False
    except Exception as ex:
        print(f"{_LOG} ❌ Lỗi gửi lệnh: {ex}", flush=True)
        return False


def check_and_maybe_deposit() -> None:
    cfg = _cfg()
    if not cfg["ENABLED"]:
        return

    balances = fetch_all_device_balances(
        cfg["BANKING_API_URL"],
        cfg_token=cfg.get("INTERNAL_DB_TOKEN") or "",
    )
    total = sum_balances(balances)
    threshold = cfg["TOTAL_BALANCE_THRESHOLD_VND"]
    device_count = len(balances)

    print(
        f"{_LOG} Tổng số dư {device_count} device (Banking): {total:,}đ "
        f"(ngưỡng > {threshold:,}đ)",
        flush=True,
    )

    if total <= threshold:
        print(f"{_LOG} Bỏ qua — chưa vượt ngưỡng", flush=True)
        return

    order = build_withdraw_order(cfg)
    amount = cfg["DEPOSIT_AMOUNT_VND"]
    print(
        f"{_LOG} 📤 Gửi 1 lệnh {amount:,}đ → {cfg['BANKING_API_URL']}/api/orders/withdraw | "
        f"orderId={order['orderId']} | NDCK={order['transferContent']}",
        flush=True,
    )
    if send_withdraw_order(cfg, order):
        print(f"{_LOG} ✅ Đã gửi lệnh thành công", flush=True)
    else:
        print(f"{_LOG} ❌ Gửi lệnh thất bại", flush=True)


def start_device_balance_overflow_scheduler() -> None:
    cfg = _cfg()
    if not cfg["ENABLED"]:
        print(f"{_LOG} Tắt (ENABLED=0 trong config)", flush=True)
        return

    interval = max(60, cfg["CHECK_INTERVAL_SECONDS"])

    def _loop() -> None:
        print(
            f"{_LOG} Mỗi {interval}s — nạp {cfg['DEPOSIT_AMOUNT_VND']:,}đ "
            f"nếu tổng số dư Banking > {cfg['TOTAL_BALANCE_THRESHOLD_VND']:,}đ "
            f"({cfg['BANKING_API_URL']})",
            flush=True,
        )
        while True:
            try:
                if not _tick_lock.acquire(blocking=False):
                    time.sleep(interval)
                    continue
                try:
                    check_and_maybe_deposit()
                finally:
                    _tick_lock.release()
            except Exception as ex:
                print(f"{_LOG} ❌ {ex}", flush=True)
            time.sleep(interval)

    threading.Thread(
        target=_loop,
        daemon=True,
        name="device-balance-overflow",
    ).start()
