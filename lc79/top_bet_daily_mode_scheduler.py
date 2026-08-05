"""
Chế độ TOP cược ngày → V2 + strategy 8 (1 lần/ngày).

Config TOP_BET_DAILY_MODE:
  ENABLED: 0 tắt, 1 bật
  CHECK_INTERVAL_SECONDS: mỗi N giây tick scheduler + kiểm tra DB (thoát V2)
  START: giờ kích hoạt hàng ngày (Asia/Ho_Chi_Minh), 1 lần/ngày sau START
  API_INTERVAL_SECONDS: mỗi N giây gọi game API lấy mốc top 500 (khi đang monitor)
  USER_COUNT: số user V2 tối đa (thiếu user thì lấy tối đa có được)
  THRESHOLD_OFFSET_VND: loại user có total_day − top500 > ngưỡng này khi chọn (vd 1_000_000)
  EXIT_GAP_MIN_VND: thoát V2 khi mọi user V2 có total_day − top500 > ngưỡng này (vd 200_000)
  API_USERNAME: user gọi game API (trống → WS đang chạy hoặc user đầu V2/PRIORITY)

Luồng:
  Sau START (chưa chạy hôm nay): API top500 + CMS (chỉ Đang Chơi / Hết Tiền) → lọc/sort → V2, strategy=8
  Monitor: bỏ V2 user không còn Đang Chơi/Hết Tiền; khi mọi user còn lại gap > EXIT_GAP_MIN_VND
    → xóa V2, strategy=3; MAX outside theo config/TIME_WINDOWS; chờ ngày hôm sau mới chọn lại.
  Ngoài phiên V2: không ép strategy/MAX liên tục — chỉ dùng config/TIME_WINDOWS.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from constants import load_config, save_config
from top_bet_daily_checker import (
    _fetch_cms_total_day_by_username,
    _fetch_top_bet_daily_list,
    _money_bet_at_top_idx,
    compute_top_bet_daily_gap_pick,
    fetch_playing_or_out_usernames,
    format_top_bet_gap_pick_report,
    v2_users_all_above_exit_gap,
)

_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_STATE_PATH = Path(__file__).resolve().parent / "top_bet_daily_state.json"
_config_lock = threading.Lock()
_last_api_fetch_mono: float = 0.0
_cached_money_500: int = 0


def _parse_hhmm(s: str, default_h: int, default_m: int) -> tuple[int, int]:
    s = (s or "").strip()
    if not s:
        return default_h, default_m
    parts = s.replace(":", " ").split()
    if len(parts) >= 2:
        try:
            return max(0, min(23, int(parts[0]))), max(0, min(59, int(parts[1])))
        except ValueError:
            pass
    if ":" in s:
        a, b = s.split(":", 1)
        try:
            return max(0, min(23, int(a))), max(0, min(59, int(b)))
        except ValueError:
            pass
    return default_h, default_m


def _mode_block(cfg: dict) -> dict:
    block = cfg.get("TOP_BET_DAILY_MODE")
    return block if isinstance(block, dict) else {}


def top_bet_daily_mode_enabled(cfg: dict) -> bool:
    try:
        return int(_mode_block(cfg).get("ENABLED", 0)) == 1
    except (TypeError, ValueError):
        return False


def _to_int(v: Any, default: int) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _today_str(now: datetime | None = None) -> str:
    if now is None:
        now = datetime.now(_TZ)
    return now.strftime("%Y-%m-%d")


def _start_time(cfg: dict) -> dt_time:
    block = _mode_block(cfg)
    sh, sm = _parse_hhmm(str(block.get("START", "22:00")), 22, 0)
    return dt_time(sh, sm)


def _past_start_today(cfg: dict, now: datetime | None = None) -> bool:
    if now is None:
        now = datetime.now(_TZ)
    return now.time() >= _start_time(cfg)


def _load_state() -> dict:
    try:
        if _STATE_PATH.is_file():
            data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_state(state: dict) -> None:
    try:
        _STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[TOP-BET-DAY] ⚠️ Không ghi state: {e}", flush=True)


def _session_active(state: dict | None = None) -> bool:
    st = state if state is not None else _load_state()
    return bool(st.get("session_active"))


def _last_pick_date(state: dict | None = None) -> str:
    st = state if state is not None else _load_state()
    return str(st.get("last_pick_date") or "").strip()


def _last_exit_date(state: dict | None = None) -> str:
    st = state if state is not None else _load_state()
    return str(st.get("last_exit_date") or "").strip()


def _set_session_active(
    active: bool,
    *,
    last_pick_date: str | None = None,
    last_exit_date: str | None = None,
) -> None:
    state = _load_state()
    state["session_active"] = active
    if last_pick_date is not None:
        state["last_pick_date"] = last_pick_date
    if last_exit_date is not None:
        state["last_exit_date"] = last_exit_date
    _save_state(state)


def _infer_session_from_config(cfg: dict) -> bool:
    """Khôi phục session sau restart nếu chưa có state file."""
    if _STATE_PATH.is_file():
        return False
    if _cfg_int(cfg, "ASSIGN_STRATEGY", 0) != _ACTIVE_STRATEGY:
        return False
    return _v2_has_any_user(cfg)


def top_bet_daily_mode_active(cfg: dict, now: datetime | None = None) -> bool:
    """ENABLED=1 và đang trong phiên V2 (strategy 8, monitor thoát)."""
    if not top_bet_daily_mode_enabled(cfg):
        return False
    state = _load_state()
    if _session_active(state):
        return True
    if _infer_session_from_config(cfg):
        _set_session_active(True, last_pick_date=_today_str(now))
        return True
    return False


def top_bet_daily_mode_forces_strategy(cfg: dict) -> bool:
    """chiaTien_Acc ép strategy 8 khi đang trong phiên V2."""
    return top_bet_daily_mode_active(cfg)


def _normalize_v2_slots(lst: List[Any], nslots: int) -> List[str]:
    out: List[str] = []
    for i in range(nslots):
        if i < len(lst):
            out.append(str(lst[i] or "").strip())
        else:
            out.append("")
    return out


def _resolve_api_username(cfg: dict) -> Optional[str]:
    block = _mode_block(cfg)
    u = str(block.get("API_USERNAME") or "").strip()
    if u:
        return u
    try:
        from session_game_total import pick_from_active_ws

        probe = pick_from_active_ws()
        if probe:
            return str(probe).strip()
    except Exception:
        pass
    for key in ("PRIORITY_USERS_V2", "PRIORITY_USERS"):
        lst = cfg.get(key) or []
        if isinstance(lst, list):
            for item in lst:
                s = str(item or "").strip()
                if s:
                    return s
    return None


def _v2_usernames_from_cfg(cfg: dict) -> List[str]:
    lst = cfg.get("PRIORITY_USERS_V2")
    if not isinstance(lst, list):
        return []
    return [str(u or "").strip() for u in lst if str(u or "").strip()]


_OUTSIDE_WINDOW_STRATEGY = 3
_ACTIVE_STRATEGY = 8
_MAX_ACTIVE_IN_WINDOW = 0


def top_bet_daily_mode_resolved_max_outside(cfg: dict, now: datetime | None = None) -> int | None:
    """ENABLED=1 và đang phiên V2 → 0; còn lại → None (config/TIME_WINDOWS)."""
    if not top_bet_daily_mode_enabled(cfg):
        return None
    if top_bet_daily_mode_active(cfg, now):
        return _MAX_ACTIVE_IN_WINDOW
    return None


def _cfg_int(cfg: dict, key: str, default: int) -> int:
    try:
        return int(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def _v2_has_any_user(cfg: dict) -> bool:
    return bool(_v2_usernames_from_cfg(cfg))


def _sync_limits_and_strategy(cfg: dict) -> bool:
    """Đồng bộ strategy=8 khi đang trong phiên V2 (MAX outside chỉ ép runtime)."""
    target_strategy = _ACTIVE_STRATEGY

    if _cfg_int(cfg, "ASSIGN_STRATEGY", 0) == target_strategy:
        return False
    cfg["ASSIGN_STRATEGY"] = target_strategy
    return save_config(cfg)


def _clear_v2_and_exit(cfg: dict) -> bool:
    """Xóa V2 + strategy 3 (MAX outside giữ theo config)."""
    v2_old = cfg.get("PRIORITY_USERS_V2")
    if not isinstance(v2_old, list):
        v2_old = []
    slots = len(v2_old) if v2_old else 0
    new_v2 = [""] * slots if slots > 0 else []

    changed = _v2_has_any_user(cfg)
    if slots > 0 and _normalize_v2_slots(v2_old, slots) != new_v2:
        changed = True
    if _cfg_int(cfg, "ASSIGN_STRATEGY", 0) != _OUTSIDE_WINDOW_STRATEGY:
        changed = True

    if not changed:
        return False

    if slots > 0:
        cfg["PRIORITY_USERS_V2"] = new_v2
    elif isinstance(cfg.get("PRIORITY_USERS_V2"), list):
        cfg["PRIORITY_USERS_V2"] = []
    cfg["ASSIGN_STRATEGY"] = _OUTSIDE_WINDOW_STRATEGY
    return save_config(cfg)


def _apply_v2_and_strategy(cfg: dict, selected: List[str], user_count: int) -> bool:
    v2_old = cfg.get("PRIORITY_USERS_V2")
    if not isinstance(v2_old, list):
        v2_old = []
    slots = max(len(v2_old), user_count, len(selected))
    new_v2 = selected + [""] * (slots - len(selected))
    new_v2 = new_v2[:slots]

    changed = _normalize_v2_slots(v2_old, slots) != new_v2
    if _cfg_int(cfg, "ASSIGN_STRATEGY", 0) != _ACTIVE_STRATEGY:
        changed = True

    if not changed:
        return False

    cfg["PRIORITY_USERS_V2"] = new_v2
    cfg["ASSIGN_STRATEGY"] = _ACTIVE_STRATEGY
    return save_config(cfg)


def _prune_v2_to_users(cfg: dict, kept: List[str]) -> bool:
    """Giữ số slot V2, chỉ còn usernames trong kept (cùng thứ tự)."""
    v2_old = cfg.get("PRIORITY_USERS_V2")
    if not isinstance(v2_old, list):
        v2_old = []
    slots = max(len(v2_old), len(kept))
    new_v2 = kept + [""] * (slots - len(kept))
    new_v2 = new_v2[:slots]
    if _normalize_v2_slots(v2_old, slots) == new_v2:
        return False
    cfg["PRIORITY_USERS_V2"] = new_v2
    return save_config(cfg)


def _should_run_daily_pick(cfg: dict, now: datetime | None = None) -> bool:
    if now is None:
        now = datetime.now(_TZ)
    if not _past_start_today(cfg, now):
        return False
    state = _load_state()
    if _session_active(state):
        return False
    return _last_pick_date(state) != _today_str(now)


def _refresh_top500_cache(cfg: dict, *, force: bool = False) -> int:
    """Gọi game API, cache moneyBet hạng 500. Trả về mốc (0 nếu thất bại)."""
    global _last_api_fetch_mono, _cached_money_500

    block = _mode_block(cfg)
    api_interval = max(1, _to_int(block.get("API_INTERVAL_SECONDS", 300), 300))
    now_mono = time.monotonic()
    if not force and _cached_money_500 > 0 and _last_api_fetch_mono > 0:
        if now_mono - _last_api_fetch_mono < api_interval:
            return _cached_money_500

    username = _resolve_api_username(cfg)
    if not username:
        return _cached_money_500

    data = _fetch_top_bet_daily_list(username)
    if not data:
        return _cached_money_500

    money_500 = _money_bet_at_top_idx(data, 500)
    if money_500:
        _cached_money_500 = money_500
        _last_api_fetch_mono = now_mono
    return _cached_money_500


def _exit_gap_min(cfg: dict) -> int:
    return max(0, _to_int(_mode_block(cfg).get("EXIT_GAP_MIN_VND", 200_000), 200_000))


def top_bet_daily_mode_daily_pick_tick(*, force: bool = False) -> None:
    """Chọn user V2 1 lần/ngày sau START."""
    with _config_lock:
        cfg = load_config()
        if not cfg or not top_bet_daily_mode_enabled(cfg):
            return
        now = datetime.now(_TZ)
        if not force and not _should_run_daily_pick(cfg, now):
            return

        block = _mode_block(cfg)
        user_count = max(1, _to_int(block.get("USER_COUNT", 8), 8))
        username = _resolve_api_username(cfg)
        if not username:
            print(
                "[TOP-BET-DAY] ⚠️ Không có API_USERNAME / user WS / V2 để gọi game API",
                flush=True,
            )
            _set_session_active(False, last_pick_date=_today_str(now))
            return

        pick = compute_top_bet_daily_gap_pick(username, user_count=user_count)
        selected = [
            str(row["username"]).strip()
            for row in pick.selected
            if row.get("username")
        ]

        today = _today_str(now)
        if pick.money_500:
            global _cached_money_500, _last_api_fetch_mono
            _cached_money_500 = pick.money_500
            _last_api_fetch_mono = time.monotonic()

        if not selected:
            print(
                "[TOP-BET-DAY] ⚠️ Không chọn được user (top500/bet-totals/lọc gap)",
                flush=True,
            )
            if pick.rule:
                print(f"[TOP-BET-DAY]    {pick.rule}", flush=True)
            _set_session_active(False, last_pick_date=today)
            return

        if _apply_v2_and_strategy(cfg, selected, user_count):
            _set_session_active(True, last_pick_date=today)
            print(
                f"[TOP-BET-DAY] ✅ Bắt đầu phiên V2 ({len(selected)}/{user_count}) + "
                f"ASSIGN_STRATEGY={_ACTIVE_STRATEGY} + "
                f"MAX outside={_MAX_ACTIVE_IN_WINDOW} (runtime): "
                f"{', '.join(selected)}",
                flush=True,
            )
            for line in format_top_bet_gap_pick_report(pick, user_count=user_count).splitlines():
                print(f"[TOP-BET-DAY] {line}", flush=True)
        else:
            _set_session_active(True, last_pick_date=today)
            print("[TOP-BET-DAY] (V2/strategy không đổi, vẫn vào phiên monitor)", flush=True)


def top_bet_daily_mode_monitor_tick() -> None:
    """Kiểm tra DB: bỏ user không còn Đang Chơi/Hết Tiền; thoát V2 khi mọi user còn lại gap > EXIT_GAP_MIN_VND."""
    with _config_lock:
        cfg = load_config()
        if not cfg or not top_bet_daily_mode_enabled(cfg):
            return
        if not top_bet_daily_mode_active(cfg):
            return

        v2_users = _v2_usernames_from_cfg(cfg)
        if not v2_users:
            _set_session_active(False)
            return

        allowed_names = fetch_playing_or_out_usernames()
        if allowed_names is not None:
            allowed = {u.lower() for u in allowed_names}
            kept = [u for u in v2_users if u.lower() in allowed]
            dropped = [u for u in v2_users if u.lower() not in allowed]
            if dropped:
                if _prune_v2_to_users(cfg, kept):
                    print(
                        f"[TOP-BET-DAY] ✂️ Loại khỏi V2 (không còn Đang Chơi/Hết Tiền): "
                        f"{', '.join(dropped)}",
                        flush=True,
                    )
                v2_users = kept
                if not v2_users:
                    today = _today_str()
                    cleared = _clear_v2_and_exit(cfg)
                    _set_session_active(False, last_exit_date=today)
                    print(
                        "[TOP-BET-DAY] 🏁 Thoát V2 — không còn user Đang Chơi/Hết Tiền trong V2",
                        flush=True,
                    )
                    if cleared:
                        print(
                            f"[TOP-BET-DAY] ASSIGN_STRATEGY={_OUTSIDE_WINDOW_STRATEGY}, "
                            f"MAX_ACTIVE_USERS_OUTSIDE_V2_V3 theo config/TIME_WINDOWS",
                            flush=True,
                        )
                    return

        money_500 = _refresh_top500_cache(cfg)
        if not money_500:
            return

        exit_gap = _exit_gap_min(cfg)
        # Gap theo bet-totals (không phụ thuộc status API) cho user V2 còn lại
        day_by = _fetch_cms_total_day_by_username()
        cms_by = {
            u.lower(): {"username": u, "total_day": day_by.get(u.lower(), 0), "gap": 0}
            for u in v2_users
        }
        all_above, details = v2_users_all_above_exit_gap(
            v2_users, money_500, exit_gap, cms_by_name=cms_by
        )
        if not all_above:
            return

        today = _today_str()
        cleared = _clear_v2_and_exit(cfg)
        already_exited_today = _last_exit_date() == today
        _set_session_active(False, last_exit_date=today)
        if not cleared and already_exited_today:
            return

        summary = ", ".join(f"{d['username']}(gap={d['gap']:,})" for d in details)
        print(
            f"[TOP-BET-DAY] 🏁 Thoát V2 — mọi user gap > {exit_gap:,} "
            f"(top500={money_500:,}): {summary}",
            flush=True,
        )
        if cleared:
            print(
                f"[TOP-BET-DAY] ASSIGN_STRATEGY={_OUTSIDE_WINDOW_STRATEGY}, "
                f"MAX_ACTIVE_USERS_OUTSIDE_V2_V3 theo config/TIME_WINDOWS",
                flush=True,
            )


def top_bet_daily_mode_limits_tick() -> None:
    """Đồng bộ strategy=8 + MAX=0 chỉ khi đang trong phiên V2."""
    with _config_lock:
        cfg = load_config()
        if not cfg or not top_bet_daily_mode_enabled(cfg):
            return
        if not top_bet_daily_mode_active(cfg):
            return
        if not _sync_limits_and_strategy(cfg):
            return
        print(
            f"[TOP-BET-DAY] ASSIGN_STRATEGY={_ACTIVE_STRATEGY}, "
            f"MAX outside={_MAX_ACTIVE_IN_WINDOW} (runtime)",
            flush=True,
        )


def top_bet_daily_mode_scheduler_loop() -> None:
    global _last_api_fetch_mono, _cached_money_500
    print(
        "[TOP-BET-DAY] Scheduler đã khởi động (CHECK_INTERVAL_SECONDS khi ENABLED=1)",
        flush=True,
    )
    while True:
        try:
            cfg = load_config() or {}
            block = _mode_block(cfg)
            check_interval = max(1, _to_int(block.get("CHECK_INTERVAL_SECONDS", 30), 30))

            if top_bet_daily_mode_enabled(cfg):
                top_bet_daily_mode_limits_tick()
                if top_bet_daily_mode_active(cfg):
                    top_bet_daily_mode_monitor_tick()
                else:
                    top_bet_daily_mode_daily_pick_tick()
            else:
                _last_api_fetch_mono = 0.0
                _cached_money_500 = 0

            time.sleep(check_interval)
        except Exception as e:
            print(f"[TOP-BET-DAY] ❌ Lỗi tick: {e}", flush=True)
            import traceback

            traceback.print_exc()
            time.sleep(30)


def start_top_bet_daily_mode_scheduler() -> None:
    threading.Thread(
        target=top_bet_daily_mode_scheduler_loop,
        daemon=True,
        name="top-bet-daily-mode",
    ).start()
