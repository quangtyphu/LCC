# -*- coding: utf-8 -*-
"""
Đọc xoso66_config.json — chỉ vài key user chỉnh; còn lại hardcode trong file này.

Chỉnh trong JSON:
  auto_deposit.amount_vnd
  auto_deposit.deposit_channel_id
  auto_deposit.deposit_channel_name
  auto_deposit.min_deposit_vnd
  game_worker.ws_account_count
  game_worker.ws_default_username  (WS CLI mặc định)
  game_worker.ws_listener_username  (nick giữ WS nghe phiên — không ngắt cap; fallback ws_default_username)
  game_worker.ws_listener_enabled  (true/false — tắt hẳn WS listener)
  auto_bet.enabled
  auto_bet.side_total_by_jackpot_enabled  (0 = cố định side_total_low_vnd; 1 = chia mức theo bậc hũ)
  auto_bet.min_jackpot_vnd
  auto_bet.jackpot_side_mid_vnd  (hũ > mức này → side_total_high_vnd; tới mức này → side_total_low_vnd)
  auto_bet.side_total_low_vnd
  auto_bet.side_total_high_vnd
  auto_bet.side_total_vnd  (fallback khi jackpot_side_mid_vnd = 0)
  auto_bet.bet_step_vnd
  auto_bet.daily_bet_cap_vnd
  auto_bet.assign_strategy  (1 hoặc 2 — xoso66_bet_assign.STRATEGY_LABELS; 2 = cược ngày thấp trước)
  auto_bet.assign_match_mode  (0 = khớp lệnh nào cược lệnh đó, pool Tài+Xỉu chung; 1 = khớp hết mới cược)
  auto_mission_reward.min_withdraw_vnd  (số dư ≥ mức này mới tự rút trước nhận thưởng)
  auto_mission_reward.withdraw_step_vnd  (bội rút, vd. 100000 → 300k/400k/500k)
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
    ("auto_deposit", "min_deposit_vnd"),
    ("game_worker", "ws_account_count"),
    ("game_worker", "ws_default_username"),
    ("game_worker", "ws_listener_username"),
    ("game_worker", "ws_listener_enabled"),
    ("auto_bet", "enabled"),
    ("auto_bet", "side_total_by_jackpot_enabled"),
    ("auto_bet", "min_jackpot_vnd"),
    ("auto_bet", "jackpot_side_mid_vnd"),
    ("auto_bet", "side_total_low_vnd"),
    ("auto_bet", "side_total_high_vnd"),
    ("auto_bet", "side_total_vnd"),
    ("auto_bet", "bet_step_vnd"),
    ("auto_bet", "daily_bet_cap_vnd"),
    ("auto_bet", "assign_strategy"),
    ("auto_bet", "assign_match_mode"),
    ("auto_bet", "PRIORITY_USERS"),
    ("auto_mission_reward", "min_withdraw_vnd"),
    ("auto_mission_reward", "withdraw_step_vnd"),
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
        "deposit_channel_id": 280,
        "deposit_channel_name": "TOPAY-Ngân hàng trực tuyến",
        "min_deposit_vnd": 1_000_000,
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
        "ws_listener_username": "quangtyphu",
        "ws_default_username": "quangtyphu",
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
        "withdraw_step_vnd": 100_000,
        "claim_between_delay_sec": 3,
        "reward_retry_delay_sec": 300,
        "reward_retry_max": 3,
        "withdraw_confirm_poll_interval_sec": 60,
        "withdraw_confirm_poll_max": 20,
        "disable_auto_bet_on_withdraw_timeout": True,
        "telegram_enabled": True,
        "worker_tick_sec": 10,
    },
    "auto_bet": {
        "enabled": True,
        "verbose_log": True,
        "log_jackpot_pick": True,
        "bootstrap_pick_sec": 30,
        "playing_game_max_age_sec": 1800,
        "side_total_by_jackpot_enabled": 1,
        "min_jackpot_vnd": 2_000_000_000,
        "jackpot_side_mid_vnd": 3_000_000_000,
        "side_total_low_vnd": 50_000,
        "side_total_high_vnd": 100_000,
        "game_ids": [2, 9, 17, 18, 19],
        "side_total_vnd": 100_000,
        "bet_step_vnd": 10_000,
        "players_per_side": 6,
        "split_dump_at_player": 6,
        "assign_strategy": 2,
        "assign_match_mode": 1,
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
    """verbose=false → không in banner [MAIN]/[API]/[GAME] khi khởi động."""
    sc = cfg.get("startup_checks") if isinstance(cfg.get("startup_checks"), dict) else {}
    if sc.get("verbose"):
        return False
    return sc.get("quiet", True)


def main_progress(msg: str) -> None:
    """Luôn in (kể cả quiet) — biết main đang làm gì, tránh màn hình trống."""
    print(msg, flush=True)
