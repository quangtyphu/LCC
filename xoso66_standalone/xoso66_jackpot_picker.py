# -*- coding: utf-8 -*-
"""
Chọn game có hũ lớn nhất >= ngưỡng config (đọc minigame_jackpots.json).

So sánh chọn game: game_id=2 chỉ tính 80% hũ; các game khác 100% (số hũ thật vẫn lưu/log).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from xoso66_minigame_catalog import DEFAULT_JACKPOT_GAME_IDS, game_by_id
from xoso66_minigame_jackpot_store import get_jackpot_store

# So sánh chọn game: game_id=2 (Tài xỉu) chỉ tính 80% hũ; các game khác 100%.
_JACKPOT_COMPARE_DISCOUNT_GAME_ID = 2
_JACKPOT_COMPARE_DISCOUNT_RATIO = 0.8


def jackpot_compare_vnd(game_id: int, money_vnd: float) -> float:
    """Giá trị hũ dùng so sánh / ngưỡng min (khác số hũ thật trên WS)."""
    if int(game_id) == _JACKPOT_COMPARE_DISCOUNT_GAME_ID:
        return float(money_vnd) * _JACKPOT_COMPARE_DISCOUNT_RATIO
    return float(money_vnd)


@dataclass
class PickedGame:
    game_id: int
    game_key: str
    game_name: str
    money_vnd: float


def _auto_bet_cfg(cfg: dict) -> dict:
    raw = cfg.get("auto_bet")
    return raw if isinstance(raw, dict) else {}


def min_jackpot_vnd(cfg: dict) -> float:
    """Ngưỡng auto_bet.min_jackpot_vnd (0 = không lọc)."""
    return float(_auto_bet_cfg(cfg).get("min_jackpot_vnd") or 0)


def side_total_by_jackpot_enabled(cfg: dict) -> bool:
    """0 = tắt (cố định side_total_low_vnd); 1 = bật chia mức theo bậc hũ."""
    raw = _auto_bet_cfg(cfg).get("side_total_by_jackpot_enabled", 0)
    try:
        return int(raw) != 0
    except (TypeError, ValueError):
        return bool(raw)


def resolve_side_total_vnd(cfg: dict, jackpot_vnd: float | None = None) -> int:
    """
    Tổng cược một bên Tài/Xỉu.

    side_total_by_jackpot_enabled=0 → side_total_low_vnd (không xét bậc hũ).
    Bật + jackpot_side_mid_vnd:
      min..mid → side_total_low_vnd; > mid → side_total_high_vnd.
    Bật nhưng mid=0 / không có jackpot → side_total_vnd tĩnh.
    """
    acfg = _auto_bet_cfg(cfg)
    low = int(acfg.get("side_total_low_vnd") or acfg.get("side_total_vnd") or 50_000)
    if not side_total_by_jackpot_enabled(cfg):
        return low
    mid = float(acfg.get("jackpot_side_mid_vnd") or 0)
    if mid > 0 and jackpot_vnd is not None:
        jp = float(jackpot_vnd)
        high = int(acfg.get("side_total_high_vnd") or 100_000)
        if jp <= mid:
            return low
        return high
    return int(acfg.get("side_total_vnd") or 100_000)


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


def focus_picked_game(cfg: dict) -> PickedGame | None:
    """
    Game để log / theo dõi phiên.
    Đủ ngưỡng → hũ cao nhất >= min; dưới ngưỡng → hũ cao nhất mọi game (sau nổ không giữ game cũ).
    """
    best = pick_best_jackpot_game(cfg)
    if best is not None:
        return best
    return highest_jackpot_game(cfg)


def last_watch_game_id(cfg: dict) -> int | None:
    """game_id theo dõi khi chưa đủ ngưỡng — luôn hũ cao nhất trong watch list."""
    picked = focus_picked_game(cfg)
    return int(picked.game_id) if picked is not None else None


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
    if jackpot_compare_vnd(top.game_id, top.money_vnd) >= min_jp:
        return False
    if str(issue or "").strip():
        return True
    print(
        f"{prefix} Chưa chơi — hũ cao nhất {top.game_name}: "
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
                try:
                    from xoso66_auto_bet import get_auto_bet_controller

                    get_auto_bet_controller().apply_playing_game(
                        top, cfg=cfg, source="chuyển theo dõi hũ cao nhất"
                    )
                except Exception:
                    pass
        _gate_last_game_id = None

    # «Chưa chơi» chỉ in khi BẮT ĐẦU PHIÊN game đang theo dõi (có issue),
    # không lặp mỗi lần file hũ đổi từ bất kỳ game nào trong watch list.


def focus_game_id(cfg: dict | None = None) -> int | None:
    """game_id lọc log WS / handler phiên — theo focus_picked_game (file hũ, không giữ game sau nổ)."""
    if cfg is None:
        from xoso66_config_util import load_config

        cfg = load_config()
    picked = focus_picked_game(cfg)
    return int(picked.game_id) if picked is not None else None


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
        compare = jackpot_compare_vnd(int(gid), money)
        if compare < min_jp:
            continue
        try:
            key, g = game_by_id(int(gid))
        except KeyError:
            continue
        name = str(row.get("name") or g.get("name") or key)
        if best is None or compare > jackpot_compare_vnd(
            best.game_id, best.money_vnd
        ):
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
        mark = "✓" if jackpot_compare_vnd(int(gid), money) >= min_jp else "·"
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
