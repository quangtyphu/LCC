# -*- coding: utf-8 -*-
"""
Auto nạp XOSO66 (LC79-style) — mặc định 100k, qua third_party_deposit_handler.

  python xoso66_auto_deposit.py -a acc1
  python xoso66_auto_deposit.py -a acc1 --amount 100000
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from queue import Empty, Queue
from typing import Any

import requests

_DIR = Path(__file__).resolve().parent
DEPOSIT_CACHE_FILE = _DIR / "data" / "deposit_pending_cache.json"
DEFAULT_AMOUNT_VND = 100_000
DEPOSIT_CACHE_DELAY_SEC = 15 * 60
DEPOSIT_QUEUE_INTERVAL_SEC = 0

_deposit_queue: Queue = Queue()
_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()
_enqueued: set[str] = set()
_enqueued_lock = threading.Lock()
_processing: set[str] = set()
_processing_lock = threading.Lock()
_last_deposit_time = 0.0
_last_deposit_lock = threading.Lock()


def _auto_deposit_cfg() -> dict:
    from xoso66_config_util import load_config

    raw = load_config().get("auto_deposit")
    return raw if isinstance(raw, dict) else {}


def handler_bind_host() -> str:
    """Host Flask bind (0.0.0.0 = nghe mọi interface)."""
    ad = _auto_deposit_cfg()
    return str(ad.get("handler_host") or "0.0.0.0").strip() or "0.0.0.0"


def handler_connect_base() -> str:
    """
    URL client gọi /create-deposit.
    Không dùng 0.0.0.0 — trên Windows requests tới http://0.0.0.0:5001 thường fail.
    """
    ad = _auto_deposit_cfg()
    port = int(ad.get("handler_port") or os.environ.get("XOSO66_DEPOSIT_HANDLER_PORT") or 5000)
    explicit = str(ad.get("handler_connect_host") or "").strip()
    if explicit:
        host = explicit
    else:
        bind = str(ad.get("handler_host") or "127.0.0.1").strip()
        if bind in ("0.0.0.0", "::", "[::]", ""):
            host = "127.0.0.1"
        else:
            host = bind
    return f"http://{host}:{port}"


def handler_base() -> str:
    """Alias — luôn dùng URL kết nối client."""
    return handler_connect_base()


def check_handler_reachable() -> tuple[bool, str]:
    url = f"{handler_connect_base()}/health"
    try:
        r = requests.get(url, timeout=5)
        if r.ok:
            return True, url
        return False, f"{url} HTTP {r.status_code}"
    except Exception as e:
        return False, f"{url} — {e}"


def log_deposit_order_compact(rep: dict[str, Any]) -> None:
    """In gọn thông tin CK sau khi tạo đơn + gửi bên thứ 3 OK."""
    if not rep.get("ok"):
        return
    from xoso66_accounts_db import username_for_log
    from xoso66_deposit import transfer_info_bank_fields

    ti = rep.get("transfer_info") if isinstance(rep.get("transfer_info"), dict) else {}
    bf = transfer_info_bank_fields(ti)
    bank = str(rep.get("bank") or bf["bank"] or "—").strip()
    acc_no = str(rep.get("account_number") or bf["account_number"] or "—").strip()
    holder = str(rep.get("account_holder") or bf["account_holder"] or "—").strip()
    ndck = str(rep.get("transfer_content") or bf["transfer_content"] or "—").strip()
    amount = int(rep.get("amount") or 0)
    aid = str(rep.get("account_id") or "")
    user = str(rep.get("username") or "").strip() or username_for_log(aid)
    order_id = rep.get("order_id")
    oid = order_id if order_id is not None else "?"

    def _field(s: str) -> str:
        s = str(s or "").strip()
        return s if s and s not in ("—", "-") else "—"

    ndck_disp = ndck if ndck and ndck not in ("—", "-") else "(trống)"

    print(f"📋 Thông tin lệnh nạp #{oid}:", flush=True)
    print(f"   Username:       {user}", flush=True)
    print(f"   Amount:         {amount:,}đ", flush=True)
    print(f"   Ngân hàng:      {_field(bank)}", flush=True)
    print(f"   STK:            {_field(acc_no)}", flush=True)
    print(f"   Chủ Tài khoản:  {_field(holder)}", flush=True)
    print(f"   NDCK:           {ndck_disp}", flush=True)
    print("-" * 60, flush=True)


def log_deposit_transfer_info(rep: dict[str, Any], *, prefix: str = "[NẠP]") -> None:
    """Alias — dùng format gọn."""
    log_deposit_order_compact(rep)


def default_amount() -> int:
    return int(_auto_deposit_cfg().get("amount_vnd") or DEFAULT_AMOUNT_VND)


def load_deposit_cache() -> dict[str, float]:
    if not DEPOSIT_CACHE_FILE.is_file():
        return {}
    try:
        return json.loads(DEPOSIT_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_deposit_cache(cache: dict[str, float]) -> None:
    DEPOSIT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEPOSIT_CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def mark_deposit_cache(account_id: str) -> None:
    cache = load_deposit_cache()
    cache[str(account_id)] = time.time()
    save_deposit_cache(cache)


def remove_from_deposit_cache(account_id: str) -> None:
    cache = load_deposit_cache()
    if str(account_id) in cache:
        del cache[str(account_id)]
        save_deposit_cache(cache)


def can_create_deposit_order(account_id: str) -> bool:
    from xoso66_deposit_orders_db import has_pending_deposit, init_deposit_orders_table
    from xoso66_accounts_db import init_db

    init_db()
    init_deposit_orders_table()

    ad = _auto_deposit_cfg()
    ttl = int(ad.get("cache_ttl_sec") or DEPOSIT_CACHE_DELAY_SEC)
    cache = load_deposit_cache()
    ts = cache.get(str(account_id))
    if ts and (time.time() - float(ts)) < ttl:
        return False
    if has_pending_deposit(str(account_id), max_age_sec=ttl):
        return False
    return True


def _deposit_queue_interval_sec() -> float:
    raw = _auto_deposit_cfg().get("queue_interval_sec")
    if raw is None:
        return float(DEPOSIT_QUEUE_INTERVAL_SEC)
    return max(0.0, float(raw))


def _wait_deposit_slot() -> None:
    """Chờ giữa hai lệnh nạp (mặc định 0 — không delay như LC79)."""
    interval = _deposit_queue_interval_sec()
    if interval <= 0:
        return
    global _last_deposit_time
    with _last_deposit_lock:
        elapsed = time.time() - _last_deposit_time
        if elapsed < interval:
            time.sleep(interval - elapsed)
        _last_deposit_time = time.time()


def perform_deposit(
    account_id: str,
    amount: int | None = None,
    *,
    verbose: bool = True,
) -> dict[str, Any]:
    """Gọi handler /create-deposit (tạo đơn site → in CK → gửi bên thứ 3)."""
    from xoso66_accounts_db import username_for_log

    user = username_for_log(account_id)
    amt = int(amount or default_amount())
    base = handler_connect_base()
    url = f"{base}/create-deposit"
    ok_h, hmsg = check_handler_reachable()
    if not ok_h:
        err = f"Handler nạp không phản hồi: {hmsg}"
        if verbose:
            print(f"[NẠP] {user} — {err}", flush=True)
        return {"ok": False, "error": err}
    try:
        r = requests.post(
            url,
            json={"account_id": account_id, "amount": amt},
            timeout=120,
        )
        try:
            body = r.json()
        except Exception:
            raw = (r.text or "")[:300]
            if raw.lstrip().lower().startswith("<!doctype") or "<html" in raw.lower():
                body = {
                    "error": (
                        f"Handler nạp crash HTTP {r.status_code} — "
                        "dừng main.py cũ và chạy lại (port 5001 có thể là process cũ)"
                    )
                }
            else:
                body = {"error": raw}
        if r.status_code == 200 and body.get("ok"):
            mark_deposit_cache(account_id)
            return body
        err = body.get("error") or f"HTTP {r.status_code}"
        if verbose:
            print(f"[NẠP] {user} — thất bại: {err}", flush=True)
            raw = body.get("raw") if isinstance(body.get("raw"), dict) else body
            if isinstance(raw, dict) and raw.get("error"):
                print(f"[NẠP]   chi tiết: {raw.get('error')}", flush=True)
        return {"ok": False, "error": err, "raw": body}
    except Exception as e:
        if verbose:
            print(f"[NẠP] {user} — lỗi kết nối: {e}", flush=True)
        return {"ok": False, "error": str(e)}


def enqueue_deposit(account_id: str, reason: str = "") -> None:
    aid = str(account_id).strip()
    if not aid:
        return
    with _enqueued_lock:
        if aid in _enqueued:
            return
        _enqueued.add(aid)
    _deposit_queue.put((aid, reason or "auto"))
    start_deposit_worker()


def _deposit_worker() -> None:
    from xoso66_accounts_db import username_for_log

    while True:
        try:
            task = _deposit_queue.get(timeout=2)
        except Empty:
            continue
        aid, reason = task if isinstance(task, tuple) else (str(task), "")
        user = username_for_log(aid)
        try:
            print(f"[AUTO-DEPOSIT] {user} | {reason}", flush=True)
            with _processing_lock:
                _processing.add(aid)
            _wait_deposit_slot()
            if not can_create_deposit_order(aid):
                continue
            rep = perform_deposit(aid)
            if not rep.get("ok"):
                print(f"[AUTO-DEPOSIT] FAIL {user}: {rep.get('error')}", flush=True)
        except Exception as e:
            print(f"[AUTO-DEPOSIT] Lỗi {user}: {e}", flush=True)
        finally:
            with _enqueued_lock:
                _enqueued.discard(aid)
            with _processing_lock:
                _processing.discard(aid)
            _deposit_queue.task_done()


def start_deposit_worker() -> None:
    global _worker_thread
    with _worker_lock:
        if _worker_thread and _worker_thread.is_alive():
            return
        _worker_thread = threading.Thread(
            target=_deposit_worker,
            name="xoso66-auto-deposit",
            daemon=True,
        )
        _worker_thread.start()


def start_handler_thread() -> threading.Thread | None:
    ad = _auto_deposit_cfg()
    if not ad.get("handler_enabled", True):
        return None
    port = int(ad.get("handler_port") or 5000)
    host = handler_bind_host()

    def _run() -> None:
        from xoso66_third_party_deposit_handler import run_handler

        run_handler(host=host, port=port)

    t = threading.Thread(target=_run, name="xoso66-deposit-handler", daemon=True)
    t.start()
    start_deposit_worker()
    return t


def _main() -> int:
    import argparse

    from xoso66_accounts_db import init_db
    from xoso66_deposit_orders_db import init_deposit_orders_table

    ap = argparse.ArgumentParser(description="Auto nạp một account")
    ap.add_argument("-a", "--account", required=True, help="account id")
    ap.add_argument("-m", "--amount", type=int, default=0, help="VND (mặc định config)")
    ap.add_argument("--handler-only", action="store_true", help="chỉ chạy Flask handler")
    args = ap.parse_args()

    init_db()
    init_deposit_orders_table()

    if args.handler_only:
        from xoso66_third_party_deposit_handler import run_handler

        run_handler()
        return 0

    amt = args.amount or default_amount()
    rep = perform_deposit(args.account, amt)
    print(json.dumps(rep, ensure_ascii=False, indent=2), flush=True)
    return 0 if rep.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(_main())
