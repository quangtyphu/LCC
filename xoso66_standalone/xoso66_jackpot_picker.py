# -*- coding: utf-8 -*-
"""Chọn game có hũ lớn nhất >= ngưỡng config (đọc minigame_jackpots.json)."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from xoso66_minigame_catalog import DEFAULT_JACKPOT_GAME_IDS, game_by_id
from xoso66_minigame_jackpot_store import get_jackpot_store


@dataclass
class PickedGame:
    game_id: int
    game_key: str
    game_name: str
    money_vnd: float


def _auto_bet_cfg(cfg: dict) -> dict:
    raw = cfg.get("auto_bet")
    return raw if isinstance(raw, dict) else {}


def jackpot_money_for_game(cfg: dict, game_id: int) -> float:
    """Hũ hiện tại của một game_id trong minigame_jackpots.json."""
    store = get_jackpot_store()
    row = (store.load().get("by_game") or {}).get(str(int(game_id))) or {}
    try:
        return float(str(row.get("money") or "0").replace(",", ""))
    except ValueError:
        return 0.0


_gate_lock = threading.Lock()
_gate_met: bool | None = None
_gate_last_game_id: int | None = None


def _fmt_ty_vnd(n: float) -> str:
    """Ví dụ 2.99 tỷ (không làm tròn 2.996 → '3 tỷ')."""
    ty = float(n) / 1_000_000_000
    if ty >= 1:
        return f"{ty:.2f} tỷ"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} triệu"
    return f"{n:,.0f}"


def last_watch_game_id(cfg: dict) -> int | None:
    """
    Game theo dõi phiên khi chưa đủ ngưỡng hũ — game cuối (playing_game.json / active).
    """
    from xoso66_playing_game_store import load_playing_game

    acfg = _auto_bet_cfg(cfg)
    max_age = float(acfg.get("playing_game_max_age_sec") or 1800)
    saved = load_playing_game(max_age_sec=max_age)
    if saved:
        try:
            return int(saved["game_id"])
        except (TypeError, ValueError):
            pass
    if acfg.get("enabled"):
        try:
            from xoso66_auto_bet import get_auto_bet_controller

            playing = get_auto_bet_controller().active_game_id()
            if playing is not None:
                return int(playing)
        except Exception:
            pass
    return None


def picked_game_from_id(cfg: dict, game_id: int) -> PickedGame | None:
    """PickedGame từ catalog + hũ file (không kiểm tra min)."""
    try:
        gid = int(game_id)
        key, g = game_by_id(gid)
    except (KeyError, TypeError, ValueError):
        return None
    money = jackpot_money_for_game(cfg, gid)
    name = str(g.get("name") or key)
    store = get_jackpot_store()
    row = (store.load().get("by_game") or {}).get(str(gid)) or {}
    if row.get("name"):
        name = str(row["name"])
    return PickedGame(
        game_id=gid,
        game_key=key,
        game_name=name,
        money_vnd=money,
    )


def log_jackpot_below_min(
    cfg: dict,
    *,
    issue: str = "",
    prefix: str = "[AUTO-BET]",
) -> bool:
    """
    In rõ hũ cao nhất so ngưỡng (vd. 1.33 tỷ < 2 tỷ).
    Trả True nếu đã in (vẫn dưới ngưỡng).
    """
    acfg = _auto_bet_cfg(cfg)
    min_jp = float(acfg.get("min_jackpot_vnd") or 0)
    if min_jp <= 0:
        return False
    top = highest_jackpot_game(cfg)
    if top is None:
        iss = f" issue={issue}" if issue else ""
        print(
            f"{prefix} Chưa đủ hũ{iss} — chưa có số liệu file/API "
            f"(cần ≥ {min_jp:,.0f} VND, {_fmt_ty_vnd(min_jp)})",
            flush=True,
        )
        return True
    if top.money_vnd >= min_jp:
        return False
    iss = f" | issue={issue}" if issue else ""
    print(
        f"{prefix} Chưa chơi{iss} — hũ cao nhất {top.game_name}: "
        f"{top.money_vnd:,.0f} ({_fmt_ty_vnd(top.money_vnd)}) "
        f"< ngưỡng {min_jp:,.0f} ({_fmt_ty_vnd(min_jp)})",
        flush=True,
    )
    return True


def sync_auto_bet_jackpot_gate(
    cfg: dict,
    *,
    issue: str = "",
    prefix: str = "[AUTO-BET]",
) -> None:
    """
    Gọi mỗi khi file hũ đổi (WS).
    Dưới ngưỡng: log «Chưa chơi». Vừa đủ ngưỡng: gán game + «▶ Chơi» (trước đây im lặng).
    """
    acfg = _auto_bet_cfg(cfg)
    if not acfg.get("enabled"):
        return
    min_jp = float(acfg.get("min_jackpot_vnd") or 0)
    if min_jp <= 0:
        return

    best = pick_best_jackpot_game(cfg)
    global _gate_met, _gate_last_game_id

    with _gate_lock:
        met = best is not None
        prev_met = _gate_met
        prev_gid = _gate_last_game_id
        _gate_met = met

    if met and best is not None:
        gid = int(best.game_id)
        crossed = not prev_met
        switched = prev_met and prev_gid is not None and int(prev_gid) != gid
        if crossed or switched:
            try:
                from xoso66_auto_bet import get_auto_bet_controller

                get_auto_bet_controller().apply_playing_game(
                    best, cfg=cfg, source="hũ đủ ngưỡng"
                )
            except Exception:
                announce_playing_game(best, cfg=cfg, source="hũ đủ ngưỡng")
            with _gate_lock:
                _gate_last_game_id = gid
            if crossed:
                iss = f" | issue={issue}" if issue else ""
                print(
                    f"{prefix} Đủ ngưỡng hũ{iss} — chờ BẮT ĐẦU PHIÊN để gán/cược "
                    f"(≥ {min_jp:,.0f} VND)",
                    flush=True,
                )
            elif switched:
                print(
                    f"{prefix} Đổi game theo hũ → {best.game_name} "
                    f"({best.money_vnd:,.0f} VND)",
                    flush=True,
                )
        return

    with _gate_lock:
        if prev_met:
            top = highest_jackpot_game(cfg)
            if top:
                print(
                    f"{prefix} Hũ tụt dưới ngưỡng — cao nhất {top.game_name}: "
                    f"{top.money_vnd:,.0f} ({_fmt_ty_vnd(top.money_vnd)}) "
                    f"< {min_jp:,.0f}",
                    flush=True,
                )
        _gate_last_game_id = None

    log_jackpot_below_min(cfg, issue=issue, prefix=prefix)


def focus_game_id(cfg: dict | None = None) -> int | None:
    """game_id để lọc log WS — game đang chơi, đủ hũ, hoặc game cuối khi chờ hũ."""
    if cfg is None:
        from xoso66_config_util import load_config

        cfg = load_config()
    acfg = _auto_bet_cfg(cfg)
    if acfg.get("enabled"):
        try:
            from xoso66_auto_bet import get_auto_bet_controller

            playing = get_auto_bet_controller().active_game_id()
            if playing is not None:
                return int(playing)
        except Exception:
            pass
        picked = pick_best_jackpot_game(cfg)
        if picked is not None:
            return picked.game_id
        watch = last_watch_game_id(cfg)
        if watch is not None:
            return int(watch)
        top = highest_jackpot_game(cfg)
        return top.game_id if top else None
    picked = pick_best_jackpot_game(cfg)
    if picked is not None:
        return picked.game_id
    top = highest_jackpot_game(cfg)
    return top.game_id if top else None


def _watch_game_ids(cfg: dict) -> list[int]:
    acfg = _auto_bet_cfg(cfg)
    watch = acfg.get("game_ids")
    if isinstance(watch, list) and watch:
        return [int(x) for x in watch]
    return list(DEFAULT_JACKPOT_GAME_IDS)


def highest_jackpot_game(cfg: dict) -> PickedGame | None:
    """Hũ cao nhất trong file (không lọc min) — dùng log khi chưa đủ ngưỡng."""
    return _pick_from_store(cfg, min_jp=0.0)


def pick_best_jackpot_game(cfg: dict) -> PickedGame | None:
    """
    Trong các game_id theo dõi, chọn hũ cao nhất nếu >= min_jackpot_vnd.
    """
    acfg = _auto_bet_cfg(cfg)
    min_jp = float(acfg.get("min_jackpot_vnd") or 0)
    return _pick_from_store(cfg, min_jp=min_jp)


def _pick_from_store(cfg: dict, *, min_jp: float) -> PickedGame | None:
    store = get_jackpot_store()
    state = store.load()
    by_game = state.get("by_game") or {}
    game_ids = _watch_game_ids(cfg)

    best: PickedGame | None = None
    for gid in game_ids:
        row = by_game.get(str(int(gid))) or {}
        try:
            money = float(str(row.get("money") or "0").replace(",", ""))
        except ValueError:
            continue
        if money < min_jp:
            continue
        try:
            key, g = game_by_id(int(gid))
        except KeyError:
            continue
        name = str(row.get("name") or g.get("name") or key)
        if best is None or money > best.money_vnd:
            best = PickedGame(
                game_id=int(gid),
                game_key=key,
                game_name=name,
                money_vnd=money,
            )
    return best


def format_jackpot_watch_status(cfg: dict) -> str:
    """Một dòng trạng thái hũ — in sau khi WS pool sẵn sàng / heartbeat."""
    acfg = _auto_bet_cfg(cfg)
    min_jp = float(acfg.get("min_jackpot_vnd") or 0)
    store = get_jackpot_store()
    by_game = (store.load().get("by_game") or {})
    parts: list[str] = []
    for gid in _watch_game_ids(cfg):
        row = by_game.get(str(int(gid))) or {}
        try:
            money = float(str(row.get("money") or "0").replace(",", ""))
        except ValueError:
            money = 0.0
        try:
            _, g = game_by_id(int(gid))
            name = str(row.get("name") or g.get("name") or gid)[:12]
        except KeyError:
            name = str(gid)
        mark = "✓" if money >= min_jp else "·"
        parts.append(f"{mark}{name} {money:,.0f}")
    best = pick_best_jackpot_game(cfg)
    top = highest_jackpot_game(cfg)
    focus = focus_game_id(cfg)
    n_ws = "?"
    try:
        from xoso66_ws_pool import get_connected_ws_accounts

        n_ws = str(len(get_connected_ws_accounts()))
    except Exception:
        pass
    playing_id = None
    if acfg.get("enabled"):
        try:
            from xoso66_auto_bet import get_auto_bet_controller

            playing_id = get_auto_bet_controller().active_game_id()
        except Exception:
            playing_id = None
    if playing_id is not None:
        try:
            _, g = game_by_id(int(playing_id))
            pname = str(g.get("name") or playing_id)
        except KeyError:
            pname = str(playing_id)
        tail = f"đang chơi: {pname} (id={playing_id})"
    elif best:
        tail = f"sẽ chơi: {best.game_name} (hũ ≥ {min_jp:,.0f})"
    elif top and acfg.get("enabled"):
        watch = last_watch_game_id(cfg)
        wname = top.game_name
        if watch is not None and watch != top.game_id:
            pg = picked_game_from_id(cfg, watch)
            if pg:
                wname = pg.game_name
        tail = (
            f"chờ hũ — cao nhất {top.game_name} {_fmt_ty_vnd(top.money_vnd)} "
            f"< {_fmt_ty_vnd(min_jp)} | theo dõi phiên: {wname}"
        )
    elif top and not acfg.get("enabled"):
        tail = (
            f"chưa đủ ngưỡng {min_jp:,.0f} — tạm theo {top.game_name} "
            f"({top.money_vnd:,.0f})"
        )
    else:
        tail = f"chờ hũ ≥ {min_jp:,.0f} (file/API chưa có số liệu)"
    return f"[WS] {n_ws} nick | " + " | ".join(parts) + f" — {tail}"
