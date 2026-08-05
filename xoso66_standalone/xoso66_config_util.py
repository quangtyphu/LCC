# -*- coding: utf-8 -*-
"""
Đọc xoso66_config.json — chỉ vài key user chỉnh; còn lại hardcode trong file này.

Chỉnh trong JSON:
  auto_deposit.amount_vnd
  auto_deposit.deposit_channel_id
  auto_deposit.deposit_channel_name
  auto_deposit.deposit_bank_id      (NH chuyển khoản — TIMEPAY bank_list, VD 33 = VPBANK)
  auto_deposit.deposit_bank_name    (tên NH nếu không set deposit_bank_id, mặc định VPBANK)
  auto_deposit.min_deposit_vnd
  game_worker.ws_account_count
  game_worker.ws_default_username  (WS CLI mặc định)
  game_worker.ws_listener_username  (nick giữ WS nghe phiên — không ngắt cap; fallback ws_default_username)
  game_worker.ws_listener_enabled  (true/false — tắt hẳn WS listener)
  game_worker.ws_fill_priority  (list Hết Tiền+Đủ ngày under-cap: 2 = cược ngày cao→thấp rồi số dư cao→thấp; 1 = đủ tiền số dư cao→thấp, thiếu tiền cược thấp→cao; 0 = số dư thấp→cao rồi cược cao→thấp)
  auto_bet.enabled
  auto_bet.side_total_by_jackpot_enabled  (0 = cố định side_total_low_vnd; 1 = cược tăng theo bậc hũ)
  auto_bet.min_jackpot_vnd  (mốc bắt đầu chơi + mốc cược base)
  auto_bet.jackpot_side_step_vnd  (hũ tăng bao nhiêu thì lên 1 bậc cược; VD 500000000)
  auto_bet.side_total_low_vnd  (cược tại min_jackpot_vnd)
  auto_bet.side_total_step_vnd  (mỗi bậc hũ → cược tăng bao nhiêu; VD 10000)
  auto_bet.side_total_vnd  (fallback khi tắt bậc hũ / thiếu step)
  auto_bet.bet_step_vnd
  auto_bet.max_bet_per_user_vnd  (mỗi acc tối đa một lệnh; 0 = không giới hạn)
  auto_bet.daily_bet_cap_vnd  (895000 ≈ điểm danh; 2695000 ≈ Cửa 1 mini game — chỉ dừng cược/Đủ ngày; claim khi chuyển Đủ ngày, nâng cap không reclaim)
  daily_bet_cap_reset  (00:05 VN → ghi lại daily_bet_cap_vnd = value_vnd; mặc định 895000)
  auto_bet.assign_strategy  (1, 2 hoặc 3 — STRATEGY_LABELS; 3 = 2 acc chênh số dư nhỏ nhất, cùng mức Tài/Xỉu)
  auto_bet.assign_match_mode  (0 = khớp lệnh nào cược lệnh đó, pool Tài+Xỉu chung; 1 = khớp hết mới cược)
  auto_bet.consolidate_min_withdraw_vnd  (strategy 3: dừng cược + hẹn rút khi số dư > mức này; mặc định 300k)
  auto_bet.consolidate_no_deposit  (strategy 3: true = không nạp mọi acc; mặc định true)
  auto_bet.consolidate_pair_max_gap_vnd  (strategy 3: chênh tối đa giữa floor bet_step của 2 acc liền kề; 0 = floor phải bằng nhau)
  auto_bet.consolidate_withdraw_delay_sec  (strategy 3: chờ trước khi rút sau Đủ ngày; mặc định 420 = 7p)
  auto_bet.consolidate_min_ws_balance_vnd  (strategy 3: sàn mở WS / fill slot; mặc định 50000)
  auto_mission_reward.min_withdraw_vnd  (số dư ≥ mức này mới rút; VD 450000 = trên 450k mới rút)
  auto_mission_reward.withdraw_step_vnd  (bước/mức rút: 300000 → mỗi lần 300k nếu max cũng 300k)
  auto_mission_reward.max_withdraw_vnd  (trần mỗi lần rút; đặt = step để luôn rút đúng 1 mức)
  auto_mission_reward.min_balance_after_withdraw_vnd  (số dư sau rút phải ≥ mức này; mặc định 50000)
  auto_mission_reward.hold_reward_above_min_balance  (0/1 — 1: số dư > min_withdraw_vnd thì bỏ rút + bỏ nhận thưởng; ≤ min vẫn nhận)
  auto_mission_reward.hold_reward_poll_max  (poll thứ N/20 chưa Hoàn tất → bật hold_reward_above_min_balance=1; mặc định 5)
  auto_mission_reward.withdraw_confirm_poll_max  (tổng poll rút 1→N; mặc định 20)

Proxy mặc định (acc không có proxy): sửa DEFAULT_PROXY bên dưới hoặc env XOSO66_DEFAULT_PROXY.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any


def configure_stdio_utf8() -> None:
    """Tránh UnicodeEncodeError khi in tiếng Việt trên Windows (cp1252)."""
    import io

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
                continue
            except Exception:
                pass
        buf = getattr(stream, "buffer", None)
        if buf is not None:
            try:
                wrapped = io.TextIOWrapper(
                    buf,
                    encoding="utf-8",
                    errors="replace",
                    line_buffering=True,
                )
                if stream is sys.stdout:
                    sys.stdout = wrapped
                else:
                    sys.stderr = wrapped
            except Exception:
                pass

    _repo = Path(__file__).resolve().parent.parent
    if str(_repo) not in sys.path:
        sys.path.insert(0, str(_repo))
    from shared.console_log import install_file_tee, install_timed_print

    install_timed_print()
    # Tee stdout/stderr → xoso66_standalone/logs/xoso66_YYYYMMDD.log
    # Tắt: XOSO66_LOG_TO_FILE=0  |  Đổi thư mục: XOSO66_LOG_DIR=...
    # Giữ: mặc định 7 ngày (XOSO66_LOG_KEEP_DAYS)
    install_file_tee(log_dir=_DIR / "logs", prefix="xoso66", keep_days=7)


def safe_print(*args: object, **kwargs: object) -> None:
    """print() không làm vỡ request API khi console Windows là cp1252."""
    import builtins

    try:
        builtins.print(*args, **kwargs)
    except UnicodeEncodeError:
        file = kwargs.get("file") or sys.stdout
        sep = str(kwargs.get("sep") or " ")
        end = str(kwargs.get("end") or "\n")
        text = sep.join(str(a) for a in args) + end
        buf = getattr(file, "buffer", None)
        if buf is not None:
            buf.write(text.encode("utf-8", errors="replace"))
            if kwargs.get("flush"):
                buf.flush()
        else:
            enc = getattr(file, "encoding", None) or "utf-8"
            file.write(text.encode(enc, errors="replace").decode(enc, errors="replace"))
            if kwargs.get("flush"):
                file.flush()

_DIR = Path(__file__).resolve().parent
from xoso66_paths import default_config_path

CONFIG_PATH = Path(os.environ.get("XOSO66_CONFIG") or str(default_config_path()))

# Acc không có proxy riêng → dùng chuỗi này (hoặc env XOSO66_DEFAULT_PROXY).
DEFAULT_PROXY = "118.70.171.104:20023:PogCLP:wSMZkU"

# Chỉ các path này đọc từ xoso66_config.json (nếu có).
USER_CONFIG_PATHS: tuple[tuple[str, ...], ...] = (
    ("auto_deposit", "amount_vnd"),
    ("auto_deposit", "deposit_channel_id"),
    ("auto_deposit", "deposit_channel_name"),
    ("auto_deposit", "deposit_bank_id"),
    ("auto_deposit", "deposit_bank_name"),
    ("auto_deposit", "min_deposit_vnd"),
    ("game_worker", "ws_account_count"),
    ("game_worker", "ws_default_username"),
    ("game_worker", "ws_listener_username"),
    ("game_worker", "ws_listener_enabled"),
    ("game_worker", "ws_fill_priority"),
    ("auto_bet", "enabled"),
    ("auto_bet", "side_total_by_jackpot_enabled"),
    ("auto_bet", "min_jackpot_vnd"),
    ("auto_bet", "jackpot_side_step_vnd"),
    ("auto_bet", "side_total_low_vnd"),
    ("auto_bet", "side_total_step_vnd"),
    ("auto_bet", "side_total_vnd"),
    ("auto_bet", "bet_step_vnd"),
    ("auto_bet", "max_bet_per_user_vnd"),
    ("auto_bet", "players_per_side"),
    ("auto_bet", "split_dump_at_player"),
    ("auto_bet", "daily_bet_cap_vnd"),
    ("daily_bet_cap_reset", "enabled"),
    ("daily_bet_cap_reset", "hour"),
    ("daily_bet_cap_reset", "minute"),
    ("daily_bet_cap_reset", "value_vnd"),
    ("auto_bet", "assign_strategy"),
    ("auto_bet", "assign_match_mode"),
    ("auto_bet", "consolidate_min_withdraw_vnd"),
    ("auto_bet", "consolidate_no_deposit"),
    ("auto_bet", "consolidate_pair_max_gap_vnd"),
    ("auto_bet", "consolidate_withdraw_delay_sec"),
    ("auto_bet", "consolidate_min_ws_balance_vnd"),
    ("auto_bet", "PRIORITY_USERS"),
    ("auto_bet", "force_game_id"),
    ("auto_mission_reward", "min_withdraw_vnd"),
    ("auto_mission_reward", "withdraw_step_vnd"),
    ("auto_mission_reward", "max_withdraw_vnd"),
    ("auto_mission_reward", "min_balance_after_withdraw_vnd"),
    ("auto_mission_reward", "hold_reward_above_min_balance"),
    ("auto_mission_reward", "hold_reward_poll_max"),
    ("auto_mission_reward", "withdraw_confirm_poll_max"),
    ("startup_checks", "startup_async"),
    ("balance_reconcile", "enabled"),
    ("balance_reconcile", "interval_min"),
    ("balance_reconcile", "parallel"),
    ("balance_reconcile", "telegram_enabled"),
    ("balance_reconcile", "min_drop_notify_vnd"),
)

HARDCODED_CONFIG: dict[str, Any] = {
    "default_proxy": DEFAULT_PROXY,
    "api_key": "doi-api-key-cms",
    "api_host": "0.0.0.0",
    "api_port": 8799,
    "captcha": {
        "enabled": True,
        "provider": "capsolver",
        "api_key": "CAP-9E805F00F18EBDFF762F217824A4AF901094F43DE4105047F59FE0D2BAB06D1B",
        "max_attempts": 3,
        "timeout_sec": 120,
    },
    "startup_checks": {
        "enabled": True,
        "scope": "status",
        "verbose": False,
        "quiet": False,
        "combined_startup": True,
        "startup_async": False,
        "parallel_max": 64,
        "startup_parallel": 16,
        "balance_enabled": True,
        "balance_status": "Đang Chơi",
        "balance_parallel": 16,
        "balance_print_each": True,
        "balance_light": True,
        "balance_relogin_on_fail": False,
        "balance_timeout_sec": 12,
        "balance_api_refresh": True,
        "minigame_token_enabled": True,
        "token_startup_mode": "ping_then_refresh",
        "token_status": "Đang Chơi",
        "token_parallel": 16,
        "token_print_each": True,
        "token_retry_count": 1,
        "token_retry_playwright": True,
        "token_refresh_workers": 8,
        "token_game_key": "taixiu_dai_loc",
        "token_fetch_ws": True,
        "token_fetch_ws_on_ping": False,
        "token_cf_playwright_on_startup": False,
        "token_refresh_playwright_on_startup": False,
    },
    "workers": {
        "session_health_enabled": True,
        "session_health_interval_sec": 300,
    },
    "auto_deposit": {
        "enabled": True,
        "amount_vnd": 1_000_000,
        "deposit_channel_id": 220,
        "deposit_channel_name": "TIMEPAY-nạp tiền bankking",
        "deposit_bank_id": 33,
        "deposit_bank_name": "VPBANK",
        "min_deposit_vnd": 1_000_000,
        "handler_enabled": True,
        "handler_host": "0.0.0.0",
        "handler_connect_host": "127.0.0.1",
        "handler_port": 5001,
        # Banking cổng HMAC (giống AZP/LC79) — POST /api/orders/withdraw trên :8888
        "third_party_url": "http://127.0.0.1:8888/api/orders/withdraw",
        "callback_url": "http://127.0.0.1:5001/callback",
        "partnerId": "xoso66",
        "partner_api_key": "_KdFrx_Vrik23ysY6GDrp-_4dmh4g3GNjOU2SHK8wUg",
        "partner_api_secret": "ttL1yTZWAQ8qd7nHVeLB4S1fL7-FbgCuea8FfMiqNzvBd9Z-OYAeGrYranYqbSOU",
        "poll_on_third_party_ok": False,
        "poll_interval_sec": 10,
        "poll_max_attempts": 100,
        "deposit_list_limit": 10,
        "queue_interval_sec": 0,
        "cache_ttl_sec": 900,
    },
    "game_worker_enabled": True,
    "game_worker_account_ids": [],
    "game_worker": {
        "ws_account_count": 12,
        "min_balance_vnd": 10_000,
        "deposit_wait_confirm": True,
        "round_start_log_delay_sec": 8,
        "round_start_balance_check_delay_sec": 10,
        "ws_pool_resync_enabled": True,
        "ws_pool_resync_interval_sec": 60,
        "ws_pool_resync_only_expand": True,
        "ws_connect_batch_size": 8,
        "ws_connect_batch_delay_sec": 0.35,
        "ws_bulk_refresh_threshold": 5,
        "ws_listener_enabled": True,
        "ws_fill_priority": 2,
        "ws_listener_account_id": "",
        "ws_listener_username": "quangtyphu",
        "ws_default_username": "quangtyphu",
        "account_status": "Đang Chơi",
        "balance_monitor_enabled": False,
        "balance_monitor_interval_sec": 90,
        "ws_vip_after_connect_enabled": True,
        "ws_vip_after_connect_claim": True,
        "ws_vip_after_connect_cooldown_sec": 3600,
        "ws_vip_after_claim_refresh_balance": True,
        "withdraw_sync_on_ws_open": True,
        "withdraw_sync_list_limit": 10,
        "withdraw_sync_days": 7,
        "withdraw_sync_on_ws_cooldown_sec": 120,
    },
    "device_balance": {
        "banking_api_url": "http://127.0.0.1:8888",
        # banking-db Node — credit XMSB* (giống CMS BANKING_CREDIT_URL / :3010)
        "banking_credit_url": "http://127.0.0.1:3010",
        "cms_api_url": "http://127.0.0.1:3000",
    },
    "auto_red_packet": {
        "enabled": True,
        "hour": 21,
        "minute": 0,
        "parallel": 5,
        "worker_tick_sec": 30,
    },
    "daily_bet_cap_reset": {
        "enabled": True,
        "hour": 0,
        "minute": 5,
        "value_vnd": 895_000,
        "worker_tick_sec": 30,
    },
    "auto_mission_reward": {
        "enabled": True,
        "initial_delay_sec": 300,
        "poll_interval_sec": 60,
        "poll_max_attempts": 15,
        "min_withdraw_vnd": 300_000,
        "withdraw_step_vnd": 100_000,
        "max_withdraw_vnd": 500_000,
        "min_balance_after_withdraw_vnd": 50_000,
        "reward_retry_delay_sec": 300,
        "reward_retry_max": 3,
        "withdraw_confirm_poll_interval_sec": 60,
        "withdraw_confirm_poll_max": 20,
        "hold_reward_poll_max": 5,
        "telegram_enabled": True,
        "worker_tick_sec": 10,
        "hold_reward_above_min_balance": 0,
    },
    "balance_reconcile": {
        "enabled": True,
        "interval_min": 60,
        "parallel": 12,
        "telegram_enabled": True,
        "min_drop_notify_vnd": 50_000,
    },
    "auto_bet": {
        "enabled": True,
        "verbose_log": True,
        "log_jackpot_pick": True,
        "bootstrap_pick_sec": 30,
        "playing_game_max_age_sec": 1800,
        "side_total_by_jackpot_enabled": 1,
        "min_jackpot_vnd": 2_000_000_000,
        "jackpot_side_step_vnd": 500_000_000,
        "side_total_low_vnd": 50_000,
        "side_total_step_vnd": 10_000,
        "force_game_id": 0,
        "game_ids": [2, 9, 17, 18, 19],
        "side_total_vnd": 100_000,
        "bet_step_vnd": 10_000,
        "max_bet_per_user_vnd": 100_000,
        "players_per_side": 10,
        "split_dump_at_player": 10,
        "assign_strategy": 2,
        "assign_match_mode": 1,
        "consolidate_min_withdraw_vnd": 300_000,
        "consolidate_no_deposit": True,
        "consolidate_pair_max_gap_vnd": 0,
        "consolidate_withdraw_delay_sec": 420,
        "consolidate_min_ws_balance_vnd": 50_000,
        "PRIORITY_USERS": [],
        "daily_bet_cap_vnd": 892_000,
        "check_balance": True,
        "assign_ws_pool_only": True,
        "assign_bets_enabled": True,
        "place_orders": True,
        "log_simulated_place": False,
        "bet_stagger_mode": "random",
        "bet_plan_after_sec": 8,
        "bet_plan_delay_min_sec": 5,
        "bet_plan_delay_max_sec": 25,
        "account_status": "Đang Chơi",
        "bet_delay_min_sec": 3,
        "bet_delay_max_sec": 12,
        "bet_place_after_sec": 15,
        "bet_stagger_per_user_sec": 1,
        "plan_deadline_sec": 10,
        "bet_stagger_min_sec": 1,
        "bet_stagger_max_sec": 1,
        "win_payout_rate": 0.98,
        "win_total_return_multiplier": 1.98,
        "result_balance_wait_sec": 12,
        "token_check_before_bet": True,
        "token_refresh_playwright_on_bet": True,
        "token_refresh_auto_on_fail": True,
        "token_validate_timeout_sec": 12,
        "token_check_parallel": 8,
        "assign_strategy_switch_cooldown_sec": 600,
        "place_order_timeout_sec": 15,
        "place_order_wall_timeout_sec": 22,
        "order_price_scale": 1,
        "bet_totals_api": "",
    },
}


def _nested_get(d: dict[str, Any], path: tuple[str, ...]) -> Any:
    cur: Any = d
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _nested_set(d: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    cur = d
    for key in path[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    cur[path[-1]] = value


def _read_user_config_file() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        example = _DIR / "xoso66_config.example.json"
        if example.is_file() and CONFIG_PATH.name == "xoso66_config.json":
            try:
                CONFIG_PATH.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
                print(f"[CONFIG] Đã tạo {CONFIG_PATH.name} từ example.", flush=True)
            except Exception:
                pass
    if not CONFIG_PATH.is_file():
        return {}
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def save_user_config_value(path: tuple[str, ...], value: Any) -> bool:
    """Ghi một key user vào xoso66_config.json (chỉ path trong USER_CONFIG_PATHS)."""
    if path not in USER_CONFIG_PATHS:
        return False
    try:
        raw = _read_user_config_file()
        if not isinstance(raw, dict):
            raw = {}
        _nested_set(raw, path, value)
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(raw, indent=2, ensure_ascii=False)
        if not text.endswith("\n"):
            text += "\n"
        CONFIG_PATH.write_text(text, encoding="utf-8")
        return True
    except Exception as e:
        print(f"[CONFIG] Không ghi {CONFIG_PATH.name}: {e}", flush=True)
        return False


def ws_default_username(cfg: dict[str, Any] | None = None) -> str:
    """Username mặc định WS (CLI)."""
    c = cfg if cfg is not None else load_config()
    gw = c.get("game_worker") if isinstance(c.get("game_worker"), dict) else {}
    env_u = (os.environ.get("XOSO66_WS_DEFAULT_USERNAME") or "").strip()
    if env_u:
        return env_u
    return str(gw.get("ws_default_username") or "quangtyphu").strip()


def ws_listener_username(cfg: dict[str, Any] | None = None) -> str:
    """Username giữ WS nghe phiên/hũ (không evict cap). Fallback ws_default_username."""
    c = cfg if cfg is not None else load_config()
    gw = c.get("game_worker") if isinstance(c.get("game_worker"), dict) else {}
    u = str(gw.get("ws_listener_username") or "").strip()
    if u:
        return u
    return ws_default_username(c)


def resolve_ws_default_account_id(cfg: dict[str, Any] | None = None) -> str:
    """account_id từ ws_default_username (rỗng nếu không có trong DB)."""
    u = ws_default_username(cfg)
    if not u:
        return ""
    from xoso66_accounts_db import get_account_by_username

    row = get_account_by_username(u)
    if not row:
        return ""
    return str(row.get("id") or "").strip()


def resolve_ws_listener_account_id(cfg: dict[str, Any] | None = None) -> str:
    """account_id nick giữ WS listener — ws_listener_account_id hoặc ws_listener_username."""
    c = cfg if cfg is not None else load_config()
    gw = c.get("game_worker") if isinstance(c.get("game_worker"), dict) else {}
    aid = str(gw.get("ws_listener_account_id") or "").strip()
    if aid:
        from xoso66_accounts_db import get_account

        if get_account(aid):
            return aid
    u = ws_listener_username(c)
    if not u:
        return ""
    from xoso66_accounts_db import get_account_by_username

    row = get_account_by_username(u)
    if not row:
        return ""
    return str(row.get("id") or "").strip()


def load_config() -> dict[str, Any]:
    """Config đầy đủ = hardcode + ghi đè các key user trong JSON."""
    cfg = copy.deepcopy(HARDCODED_CONFIG)
    raw = _read_user_config_file()
    for path in USER_CONFIG_PATHS:
        val = _nested_get(raw, path)
        if val is not None:
            _nested_set(cfg, path, val)
    # default_proxy: env > hardcode (không đọc từ JSON)
    env_px = (os.environ.get("XOSO66_DEFAULT_PROXY") or "").strip()
    if env_px:
        cfg["default_proxy"] = env_px
    return cfg


def hardcoded_default_proxy() -> str:
    env = (os.environ.get("XOSO66_DEFAULT_PROXY") or "").strip()
    if env:
        return env
    return DEFAULT_PROXY


def startup_quiet(cfg: dict) -> bool:
    """verbose=true hoặc quiet=false → in banner [MAIN]/[API]/[GAME] khi khởi động."""
    sc = cfg.get("startup_checks") if isinstance(cfg.get("startup_checks"), dict) else {}
    if sc.get("verbose"):
        return False
    return bool(sc.get("quiet", False))


def startup_async_enabled(cfg: dict) -> bool:
    """True → startup checks chạy nền, không chặn WS worker."""
    sc = cfg.get("startup_checks") if isinstance(cfg.get("startup_checks"), dict) else {}
    if bool(sc.get("startup_async")):
        return True
    return str(os.environ.get("XOSO66_STARTUP_ASYNC", "")).strip().lower() in (
        "1",
        "true",
        "yes",
    )


def main_progress(msg: str) -> None:
    """Luôn in (kể cả quiet) — biết main đang làm gì, tránh màn hình trống."""
    print(msg, flush=True)
