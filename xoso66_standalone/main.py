#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XOSO66 — entry point (một file chạy tất cả, giống LC79 main.py).

  cd xoso66_standalone
  python main.py

Khởi động:
  - Startup checks: balance + token mọi acc «Đang Chơi» (xoso66_startup_checks.py)
  - SQLite CMS (Documents/CMS/game_data/xoso66.db)
  - CMS API Node (QuanLyChrome) hoặc FastAPI backup (:8799)
  - Worker nền (session health) + WS mini-game (game_worker_enabled)

CMS gọi API tại http://<api_host>:<api_port>  (mặc định 0.0.0.0:8799)
Header: X-API-Key: <api_key>
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import threading

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
import time
from pathlib import Path

# Đảm bảo import module trong cùng thư mục
_ROOT = Path(__file__).resolve().parent
os.chdir(_ROOT)
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from xoso66_paths import apply_default_env

apply_default_env()
os.environ.setdefault("PYTHONUNBUFFERED", "1")
from xoso66_config_util import configure_stdio_utf8

configure_stdio_utf8()

from xoso66_accounts_db import init_db, list_accounts
from xoso66_config_util import load_config, main_progress, startup_quiet
from xoso66_shutdown import request_stop, stopping

_SHUTDOWN_JOIN_SEC = float(os.environ.get("XOSO66_SHUTDOWN_JOIN_SEC", "20"))

# Trạng thái acc được worker session quét
_ACTIVE_STATUSES = frozenset(
    {
        "active",
        "registered",
        "fund_password_ok",
        "bank_linked",
        "migrated",
        "new",
    }
)

_bg_threads: list[threading.Thread] = []


def _cfg_workers() -> dict:
    cfg = load_config()
    w = cfg.get("workers")
    return w if isinstance(w, dict) else {}


def _track_thread(t: threading.Thread) -> threading.Thread:
    _bg_threads.append(t)
    return t


def run_api_thread(host: str, port: int, *, quiet: bool = False) -> None:
    from xoso66_api import run_api_server

    if not quiet:
        ui_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        print(f"[API] XOSO66 http://{host}:{port}", flush=True)
        print(f"[API] Giao diện: http://{ui_host}:{port}/  (không cần CMS server.js)", flush=True)
        print("[API] Docs: /docs  |  Health: /api/health", flush=True)
    run_api_server(host, port)


def worker_session_health(interval_sec: int, *, quiet: bool = False) -> None:
    """Định kỳ login/refresh session cho acc trong DB (cần proxy trên acc hoặc default_proxy)."""
    from xoso66_accounts_db import username_for_log
    from xoso66_proxy import resolve_proxy
    from xoso66_session import ensure_session, session_health

    if not quiet:
        print(f"[WORKER] Session health mỗi {interval_sec}s", flush=True)
    while not stopping():
        try:
            rows = list_accounts()
            for row in rows:
                if stopping():
                    break
                st = str(row.get("status") or "")
                if st and st not in _ACTIVE_STATUSES:
                    continue
                aid = str(row.get("id") or "")
                if not aid:
                    continue
                if not resolve_proxy(row):
                    continue
                try:
                    acc = ensure_session(aid)
                    h = session_health(acc)
                    if not h.get("ok"):
                        print(f"[WORKER] {username_for_log(aid)} session yếu: {h}", flush=True)
                except Exception as e:
                    if not stopping():
                        print(f"[WORKER] {username_for_log(aid)}: {e}", flush=True)
        except Exception as e:
            if not stopping():
                print(f"[WORKER] Lỗi vòng quét: {e}", flush=True)
        for _ in range(max(1, interval_sec)):
            if stopping():
                break
            time.sleep(1)
    print("[WORKER] Session health đã dừng.", flush=True)


def worker_auto_bet() -> threading.Thread | None:
    """Chọn game theo hũ + cược khi WS báo BẮT ĐẦU PHIÊN (cần game_worker_enabled)."""
    cfg = load_config()
    ab = cfg.get("auto_bet")
    if not isinstance(ab, dict) or not ab.get("enabled"):
        return None
    if not cfg.get("game_worker_enabled"):
        print(
            "[AUTO-BET] Bật auto_bet nhưng game_worker_enabled=false — "
            "cần WS worker để nhận phiên.",
            flush=True,
        )
        return None
    from xoso66_auto_bet import start_auto_bet_thread

    ab = cfg.get("auto_bet") if isinstance(cfg.get("auto_bet"), dict) else {}
    if ab.get("assign_bets_enabled"):
        print("[AUTO-BET] Worker: chọn game theo hũ + chia cược khi BẮT ĐẦU PHIÊN.", flush=True)
    else:
        print(
            "[AUTO-BET] Worker: đọc hũ → chọn game → chờ BẮT ĐẦU PHIÊN (chưa chia cược).",
            flush=True,
        )
    return start_auto_bet_thread()


def worker_minigame_ws() -> threading.Thread | None:
    """N acc balance cao + Đang Chơi → WS: hũ + BẮT ĐẦU PHIÊN + KẾT QUẢ."""
    cfg = load_config()
    if not cfg.get("game_worker_enabled"):
        return None
    from xoso66_accounts_db import usernames_for_log
    from xoso66_config_util import main_progress, startup_quiet
    from xoso66_minigame_ws_worker import start_ws_worker_thread
    from xoso66_ws_pool import prepare_ws_pool

    if not startup_quiet(cfg):
        print(
            "[GAME] WS worker: mọi acc «Đang Chơi» (có proxy) — "
            "jackpot + BẮT ĐẦU PHIÊN + kết quả; thêm nick thiếu ở phiên mới",
            flush=True,
        )
    else:
        main_progress("[GAME] Chuẩn bị WS «Đang Chơi» trên luồng nền…")

    def _prepare_and_run() -> None:
        try:
            account_ids = prepare_ws_pool(cfg)
            preview = ", ".join(usernames_for_log(account_ids[:5]))
            suffix = "…" if len(account_ids) > 5 else ""
            main_progress(
                f"[GAME] WS worker: đang kết nối {len(account_ids)} nick "
                f"({preview}{suffix})"
            )
            from xoso66_minigame_ws_worker import run_ws_worker_blocking

            run_ws_worker_blocking(
                account_ids,
                ws_count=len(account_ids),
                refresh_before_connect=True,
            )
        except KeyboardInterrupt:
            pass
        except Exception as e:
            print(f"[GAME] WS worker lỗi: {e}", flush=True)

    t = threading.Thread(target=_prepare_and_run, name="xoso66-ws-prepare", daemon=False)
    t.start()
    return t


def _graceful_shutdown() -> None:
    if stopping():
        return
    print("\n[MAIN] Ctrl+C — đang dừng WS + API + workers...", flush=True)
    request_stop()

    alive = [t for t in _bg_threads if t.is_alive()]
    if not alive:
        print("[MAIN] Đã dừng.", flush=True)
        return

    deadline = time.time() + _SHUTDOWN_JOIN_SEC
    for t in alive:
        remain = max(0.1, deadline - time.time())
        t.join(timeout=remain)
        if t.is_alive():
            if t.daemon:
                print(
                    f"[MAIN] {t.name} daemon — bỏ chờ (process thoát)",
                    flush=True,
                )
            else:
                print(
                    f"[MAIN] {t.name} chưa thoát sau {_SHUTDOWN_JOIN_SEC:.0f}s",
                    flush=True,
                )
        else:
            print(f"[MAIN] {t.name} đã dừng.", flush=True)

    print("[MAIN] Đã dừng.", flush=True)


def main() -> int:
    cfg = load_config()
    init_db()

    host = str(os.environ.get("XOSO66_API_HOST") or cfg.get("api_host") or "0.0.0.0")
    port = int(os.environ.get("XOSO66_API_PORT") or cfg.get("api_port") or 8799)
    workers = _cfg_workers()

    def _sig_handler(_signum: int, _frame: object) -> None:
        from xoso66_shutdown import request_stop

        request_stop()
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sig_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _sig_handler)

    quiet = startup_quiet(cfg)

    if not quiet:
        from xoso66_accounts_db import DB_PATH

        print("=" * 60, flush=True)
        print("XOSO66 — khởi động (main.py)", flush=True)
        print(f"  DB: {DB_PATH}", flush=True)
        print(f"  Config: {os.environ.get('XOSO66_CONFIG', 'xoso66_config.json')}", flush=True)
        print("=" * 60, flush=True)

    if cfg.get("game_worker_enabled") and not str(
        os.environ.get("XOSO66_SKIP_WS_PRIME", "")
    ).strip() in ("1", "true", "yes"):
        from xoso66_ws_pool import prime_ws_pool_selection, sync_exhausted_dang_choi_to_du_ngay

        try:
            sync_exhausted_dang_choi_to_du_ngay(cfg)
            prime_ws_pool_selection(cfg)
        except Exception as e:
            print(f"[MAIN] Chưa chọn được pool WS: {e}", flush=True)

    from xoso66_runtime_status import log_startup_services

    log_startup_services(cfg)

    api_thread = _track_thread(
        threading.Thread(
            target=run_api_thread,
            args=(host, port),
            kwargs={"quiet": quiet},
            daemon=False,
            name="xoso66-api",
        )
    )
    api_thread.start()
    time.sleep(0.8)

    scfg = cfg.get("startup_checks") if isinstance(cfg.get("startup_checks"), dict) else {}
    startup_async = bool(scfg.get("startup_async")) or str(
        os.environ.get("XOSO66_STARTUP_ASYNC", "")
    ).strip().lower() in ("1", "true", "yes")

    def _run_startup_checks() -> None:
        from xoso66_startup_checks import run_startup_checks_for_playing_accounts

        run_startup_checks_for_playing_accounts(cfg)

    try:
        if startup_async:
            threading.Thread(
                target=_run_startup_checks,
                daemon=True,
                name="xoso66-startup-checks",
            ).start()
            if not quiet:
                print(
                    "[STARTUP] Kiểm tra balance + token «Đang Chơi» chạy nền (API đã lên).",
                    flush=True,
                )
        else:
            if not quiet:
                print(
                    "[STARTUP] Kiểm tra balance + token «Đang Chơi» (API đã lên, chờ xong rồi mở worker)…",
                    flush=True,
                )
            _run_startup_checks()
    except KeyboardInterrupt:
        print("\n[MAIN] Hủy trong startup checks.", flush=True)
        return 130

    ad = cfg.get("auto_deposit")
    if isinstance(ad, dict) and ad.get("enabled"):
        from xoso66_auto_deposit import start_handler_thread

        dep_t = start_handler_thread()
        if dep_t is not None:
            _track_thread(dep_t)
            amt = int(ad.get("amount_vnd") or 100_000)
            port = int(ad.get("handler_port") or 5000)
            cb = str(ad.get("callback_url") or f"http://127.0.0.1:{port}/callback")
            from xoso66_auto_deposit import handler_connect_base

            if not quiet:
                print(
                    f"[AUTO-DEPOSIT] Bật — {amt:,}đ/lệnh | handler {handler_connect_base()} "
                    f"(bind :{port}) | poll {ad.get('poll_interval_sec', 10)}s × "
                    f"{ad.get('poll_max_attempts', 100)}",
                    flush=True,
                )
                tp_url = str(ad.get("third_party_url") or "")
                if tp_url:
                    print(f"[AUTO-DEPOSIT] Bên thứ 3: {tp_url}", flush=True)
                print(f"[AUTO-DEPOSIT] Callback (bên thứ 3 gọi về): {cb}", flush=True)
        time.sleep(0.5)
    elif not quiet:
        print("[AUTO-DEPOSIT] TẮT (auto_deposit.enabled=false)", flush=True)

    amr = cfg.get("auto_mission_reward")
    if isinstance(amr, dict) and amr.get("enabled"):
        from xoso66_auto_mission_reward import start_auto_mission_reward_thread

        amr_t = start_auto_mission_reward_thread(quiet=quiet)
        if amr_t is not None:
            _track_thread(amr_t)
    elif not quiet:
        print("[AUTO-MISSION] TẮT (auto_mission_reward.enabled=false)", flush=True)

    if workers.get("session_health_enabled", True):
        interval = int(workers.get("session_health_interval_sec") or 300)
        _track_thread(
            threading.Thread(
                target=worker_session_health,
                args=(interval,),
                kwargs={"quiet": quiet},
                daemon=False,
                name="xoso66-session-worker",
            )
        ).start()
    elif not quiet:
        print("[WORKER] Session health TẮT — chỉ API CMS (đăng ký/nạp/rút khi CMS gọi)", flush=True)

    ab = cfg.get("auto_bet")
    ab_enabled = isinstance(ab, dict) and ab.get("enabled")
    ab_thread = None
    if ab_enabled and cfg.get("game_worker_enabled"):
        from xoso66_auto_bet import setup_auto_bet_handlers, init_playing_game

        setup_auto_bet_handlers()
        init_playing_game(cfg, source="khởi động", wait_sec=0)
        ab_thread = worker_auto_bet()
        if ab_thread is not None:
            _track_thread(ab_thread)

    try:
        ws_thread = worker_minigame_ws()
        if ws_thread is not None:
            _track_thread(ws_thread)
        elif not cfg.get("game_worker_enabled") and not quiet:
            print("[GAME] TẮT (game_worker_enabled=false trong config)", flush=True)
    except KeyboardInterrupt:
        print("\n[MAIN] Hủy khi chuẩn bị WS / nạp.", flush=True)
        _graceful_shutdown()
        return 130

    if ab_thread is None:
        ab_thread = worker_auto_bet()
    if ab_thread is not None and ab_thread not in _bg_threads:
        _track_thread(ab_thread)
    else:
        ab = cfg.get("auto_bet")
        if not (isinstance(ab, dict) and ab.get("enabled")) and not quiet:
            print(
                "[AUTO-BET] TẮT — chỉ chọn user WS + nạp (bật lại khi sẵn sàng đặt cược)",
                flush=True,
            )

    ui_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    main_progress(
        f"[MAIN] Sẵn sàng — API http://{ui_host}:{port}/ | Ctrl+C dừng"
    )
    if not quiet:
        print(
            "[MAIN] API: provision | POST /api/deposit | POST /api/withdraw | UI /",
            flush=True,
        )

    try:
        while True:
            if stopping():
                break
            if not api_thread.is_alive() and not any(t.is_alive() for t in _bg_threads):
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        _graceful_shutdown()

    return 0 if not stopping() else 130


if __name__ == "__main__":
    raise SystemExit(main())
