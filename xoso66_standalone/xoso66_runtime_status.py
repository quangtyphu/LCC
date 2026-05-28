# -*- coding: utf-8 -*-
"""In trạng thái workers / hũ / config khi khởi động main.py."""

from __future__ import annotations

from typing import Any

from xoso66_config_util import CONFIG_PATH


def _ab(cfg: dict) -> dict:
    raw = cfg.get("auto_bet")
    return raw if isinstance(raw, dict) else {}


def log_jackpot_snapshot(cfg: dict, *, prefix: str = "[JACKPOT]") -> None:
    from xoso66_minigame_catalog import DEFAULT_JACKPOT_GAME_IDS, GAME_ID_LABELS
    from xoso66_minigame_jackpot_store import get_jackpot_store
    from xoso66_jackpot_picker import jackpot_compare_vnd, pick_best_jackpot_game

    acfg = _ab(cfg)
    watch = acfg.get("game_ids")
    if isinstance(watch, list) and watch:
        game_ids = [int(x) for x in watch]
    else:
        game_ids = list(DEFAULT_JACKPOT_GAME_IDS)

    store = get_jackpot_store()
    state = store.load()
    by_game = state.get("by_game") or {}
    min_jp = float(acfg.get("min_jackpot_vnd") or 0)

    print(f"{prefix} File: {store.state_path}", flush=True)
    if not by_game:
        print(
            f"{prefix} (trống — cần WS worker chạy để nhận jackpot_money)",
            flush=True,
        )
    for gid in sorted(game_ids):
        row = by_game.get(str(gid)) or {}
        money = row.get("money", "—")
        name = row.get("name") or GAME_ID_LABELS.get(gid, "")
        ok = ""
        try:
            raw = float(str(money).replace(",", ""))
            if jackpot_compare_vnd(gid, raw) >= min_jp:
                ok = " ≥ ngưỡng"
        except ValueError:
            pass
        print(f"{prefix}   game_id={gid} ({name}): {money}{ok}", flush=True)

    picked = pick_best_jackpot_game(cfg)
    if picked:
        print(
            f"{prefix} → Chọn chơi: game_id={picked.game_id} ({picked.game_name}) "
            f"hũ={picked.money_vnd:,.0f}",
            flush=True,
        )
    elif min_jp > 0:
        from xoso66_jackpot_picker import highest_jackpot_game, log_jackpot_below_min

        top = highest_jackpot_game(cfg)
        if top:
            log_jackpot_below_min(cfg, prefix=prefix)
        else:
            print(f"{prefix} → Chưa có game ≥ {min_jp:,.0f} VND", flush=True)


def preview_ws_accounts(cfg: dict) -> list[str]:
    from xoso66_ws_pool import select_ws_account_ids, ws_account_count

    try:
        return select_ws_account_ids(cfg)
    except Exception as e:
        print(f"[GAME] Không đọc được acc «Đang Chơi» cho WS: {e}", flush=True)
        return []


def log_startup_services(cfg: dict) -> None:
    """Banner: worker nào bật/tắt và cần sửa config gì."""
    from xoso66_config_util import startup_quiet

    ab = _ab(cfg)
    gw = bool(cfg.get("game_worker_enabled"))
    ab_on = bool(ab.get("enabled"))
    workers = cfg.get("workers") if isinstance(cfg.get("workers"), dict) else {}

    if startup_quiet(cfg):
        if not gw and not ab_on:
            print(
                "[MAIN] ⚠ Chỉ có API + startup checks — KHÔNG WS / KHÔNG auto-bet.",
                flush=True,
            )
        elif ab_on and not gw:
            print(
                "[MAIN] ⚠ auto_bet bật nhưng game_worker_enabled=false → không có phiên WS.",
                flush=True,
            )
        return

    print("[MAIN] ── Workers / mini-game ──", flush=True)
    print(f"[MAIN]   Config: {CONFIG_PATH}", flush=True)
    from xoso66_ws_pool import min_balance_for_ws, ws_account_count

    n_dep = ws_account_count(cfg) if gw else 0
    print(
        f"[MAIN]   game_worker_enabled = {gw}  "
        f"(WS: hết «Đang Chơi»; nạp khi thiếu {n_dep} nick >= {min_balance_for_ws(cfg):,})",
        flush=True,
    )
    assign_on = bool(ab.get("assign_bets_enabled", False))
    print(
        f"[MAIN]   auto_bet.enabled = {ab_on}  "
        f"({'chia cược' if assign_on else 'chọn game + chờ phiên'})",
        flush=True,
    )
    if ab_on:
        print(
            f"[MAIN]     assign_bets_enabled = {assign_on}  "
            f"(false = chỉ BẮT ĐẦU PHIÊN, chưa gán acc)",
            flush=True,
        )
        if assign_on:
            from xoso66_bet_assign import ASSIGN_MATCH_MODE_LABELS, assign_match_mode

            mm = assign_match_mode(ab)
            print(
                f"[MAIN]     assign_match_mode = {mm}  "
                f"({ASSIGN_MATCH_MODE_LABELS.get(mm, '?')})",
                flush=True,
            )
            print(
                f"[MAIN]     place_orders = {ab.get('place_orders', False)}  "
                f"(false = chỉ in kế hoạch, không HTTP cược)",
                flush=True,
            )
        print(
            f"[MAIN]     hũ từ data/minigame_jackpots.json | "
            f"ngưỡng ≥ {float(ab.get('min_jackpot_vnd') or 0):,.0f} VND",
            flush=True,
        )
    print(
        f"[MAIN]   session_health = {workers.get('session_health_enabled', True)}",
        flush=True,
    )

    if not gw and not ab_on:
        print(
            "[MAIN] ⚠ Chỉ có API + startup checks — KHÔNG WS / KHÔNG auto-bet.",
            flush=True,
        )
        print(
            '[MAIN]   Sửa xoso66_config.json: "game_worker_enabled": true, '
            '"auto_bet": {"enabled": true, ...}',
            flush=True,
        )
        return

    if ab_on and not gw:
        print(
            "[MAIN] ⚠ auto_bet bật nhưng game_worker_enabled=false → không có phiên WS.",
            flush=True,
        )

    if gw:
        ids = preview_ws_accounts(cfg)
        if ids:
            from xoso66_accounts_db import usernames_for_log

            print(f"[MAIN]   WS account: {', '.join(usernames_for_log(ids))}", flush=True)

    if ab_on:
        print(
            "[MAIN]   Log cược: WS kết nối / bắt đầu đặt cược / kết quả (không spam bảng hũ)",
            flush=True,
        )

    if not gw:
        print(
            '[MAIN]   Bật WS: "game_worker_enabled": true rồi khởi động lại main.py',
            flush=True,
        )
