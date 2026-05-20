# -*- coding: utf-8 -*-
"""
Đọc xoso66_config.json — chỉ vài key user chỉnh; còn lại hardcode trong file này.

Chỉnh trong JSON:
  auto_deposit.amount_vnd
  game_worker.ws_account_count
  auto_bet.enabled
  auto_bet.min_jackpot_vnd
  auto_bet.side_total_vnd
  auto_bet.bet_step_vnd
  auto_bet.daily_bet_cap_vnd
  auto_bet.assign_strategy  (1 hoặc 2 — xoso66_bet_assign.STRATEGY_LABELS)
  auto_mission_reward.min_withdraw_vnd  (số dư ≥ mức này mới tự rút trước nhận thưởng)
  auto_mission_reward.claim_between_delay_sec  (giây giữa 2 lần POST reward liên tiếp)

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
    if not hasattr(sys.stdout, "reconfigure"):
        return
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass

_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("XOSO66_CONFIG", _DIR / "xoso66_config.json"))

# Acc không có proxy riêng → dùng chuỗi này (hoặc env XOSO66_DEFAULT_PROXY).
DEFAULT_PROXY = "118.70.171.104:20023:PogCLP:wSMZkU"

# Chỉ các path này đọc từ xoso66_config.json (nếu có).
USER_CONFIG_PATHS: tuple[tuple[str, ...], ...] = (
    ("auto_deposit", "amount_vnd"),
    ("game_worker", "ws_account_count"),
    ("auto_bet", "enabled"),
    ("auto_bet", "min_jackpot_vnd"),
    ("auto_bet", "side_total_vnd"),
    ("auto_bet", "bet_step_vnd"),
    ("auto_bet", "daily_bet_cap_vnd"),
    ("auto_bet", "assign_strategy"),
    ("auto_mission_reward", "min_withdraw_vnd"),
    ("auto_mission_reward", "claim_between_delay_sec"),
)

HARDCODED_CONFIG: dict[str, Any] = {
    "default_proxy": DEFAULT_PROXY,
    "api_key": "doi-api-key-cms",
    "api_host": "0.0.0.0",
    "api_port": 8799,
    "startup_checks": {
        "enabled": True,
        "scope": "status",
        "verbose": False,
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
        "token_retry_playwright": False,
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
        "amount_vnd": 100_000,
        "handler_enabled": True,
        "handler_host": "0.0.0.0",
        "handler_connect_host": "127.0.0.1",
        "handler_port": 5001,
        "third_party_url": "http://localhost:8888/api/deposit",
        "callback_url": "http://127.0.0.1:5001/callback",
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
        "ws_pool_resync_interval_sec": 30,
        "ws_pool_resync_only_expand": True,
        "ws_connect_batch_size": 8,
        "ws_connect_batch_delay_sec": 0.35,
        "ws_bulk_refresh_threshold": 5,
        "ws_listener_enabled": True,
        "ws_listener_account_id": "",
        "account_status": "Đang Chơi",
        "balance_monitor_enabled": False,
        "balance_monitor_interval_sec": 90,
        "ws_vip_after_connect_enabled": True,
        "ws_vip_after_connect_claim": True,
        "ws_vip_after_connect_cooldown_sec": 3600,
        "ws_vip_after_claim_refresh_balance": True,
    },
    "device_balance": {
        "banking_api_url": "http://127.0.0.1:8888",
        "cms_api_url": "http://127.0.0.1:3000",
    },
    "auto_mission_reward": {
        "enabled": True,
        "initial_delay_sec": 300,
        "poll_interval_sec": 60,
        "poll_max_attempts": 15,
        "min_withdraw_vnd": 300_000,
        "claim_between_delay_sec": 3,
        "reward_retry_delay_sec": 300,
        "reward_retry_max": 3,
        "withdraw_confirm_poll_interval_sec": 60,
        "withdraw_confirm_poll_max": 20,
        "telegram_enabled": True,
        "worker_tick_sec": 10,
    },
    "auto_bet": {
        "enabled": True,
        "verbose_log": True,
        "log_jackpot_pick": True,
        "bootstrap_pick_sec": 30,
        "playing_game_max_age_sec": 1800,
        "min_jackpot_vnd": 2_500_000_000,
        "game_ids": [2, 9, 17, 18, 19],
        "side_total_vnd": 100_000,
        "bet_step_vnd": 10_000,
        "players_per_side": 10,
        "split_dump_at_player": 6,
        "assign_strategy": 2,
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
        "bet_place_after_sec": 12,
        "bet_stagger_per_user_sec": 1,
        "plan_deadline_sec": 10,
        "bet_stagger_min_sec": 1,
        "bet_stagger_max_sec": 1,
        "win_payout_rate": 0.98,
        "win_total_return_multiplier": 1.98,
        "result_balance_wait_sec": 12,
        "token_check_before_bet": True,
        "token_refresh_playwright_on_bet": False,
        "token_validate_timeout_sec": 12,
        "token_check_parallel": 8,
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
    """verbose=false → không in banner [MAIN]/[API]/[GAME] khi khởi động."""
    sc = cfg.get("startup_checks") if isinstance(cfg.get("startup_checks"), dict) else {}
    if sc.get("verbose"):
        return False
    return sc.get("quiet", True)


def main_progress(msg: str) -> None:
    """Luôn in (kể cả quiet) — biết main đang làm gì, tránh màn hình trống."""
    print(msg, flush=True)
