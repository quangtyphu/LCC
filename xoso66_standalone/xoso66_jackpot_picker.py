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
    """0 = tắt (cố định side_total_low_vnd); 1 = bật cược tăng theo bậc hũ."""
    raw = _auto_bet_cfg(cfg).get("side_total_by_jackpot_enabled", 0)
    try:
        return int(raw) != 0
    except (TypeError, ValueError):
        return bool(raw)


def resolve_side_total_vnd(
    cfg: dict,
    jackpot_vnd: float | None = None,
    *,
    game_id: int | None = None,
) -> int:
    """
    Tổng cược một bên Tài/Xỉu.

    side_total_by_jackpot_enabled=0 → side_total_low_vnd (không xét bậc hũ).
    Bật:
      Bắt đầu tại min_jackpot_vnd với side_total_low_vnd.
      Mỗi lần hũ tăng jackpot_side_step_vnd → cược tăng side_total_step_vnd.
      VD min=1.5 tỷ, low=50k, jp_step=500tr, bet_step=10k:
        1.5 tỷ→50k, 2 tỷ→60k, 2.5 tỷ→70k, 3 tỷ→80k, ...
      game_id=2 (Tài xỉu): so bậc bằng hũ × 80% (jackpot_compare_vnd), giống min_jackpot_vnd.
    Bật nhưng thiếu jackpot / step=0 → side_total_low_vnd.
    """
    acfg = _auto_bet_cfg(cfg)
    low = int(acfg.get("side_total_low_vnd") or acfg.get("side_total_vnd") or 50_000)
    if not side_total_by_jackpot_enabled(cfg):
        return low
    jp_step = float(acfg.get("jackpot_side_step_vnd") or 0)
    bet_step = int(acfg.get("side_total_step_vnd") or 0)
    if jp_step <= 0 or bet_step <= 0 or jackpot_vnd is None:
        return low
    jp = float(jackpot_vnd)
    compare_jp = jackpot_compare_vnd(int(game_id), jp) if game_id is not None else jp
    min_jp = float(acfg.get("min_jackpot_vnd") or 0)
    steps = int(max(0.0, (compare_jp - min_jp) // jp_step))
    return low + steps * bet_step


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


def force_game_id(cfg: dict) -> int | None:
    """auto_bet.force_game_id > 0 → ép chơi game đó; 0/rỗng → chọn theo hũ như cũ."""
    raw = _auto_bet_cfg(cfg).get("force_game_id")
    if raw is None or raw == "":
        return None
    try:
        gid = int(raw)
    except (TypeError, ValueError):
        return None
    return gid if gid > 0 else None


def forced_picked_game(cfg: dict) -> PickedGame | None:
    gid = force_game_id(cfg)
    if gid is None:
        return None
    return picked_game_from_id(cfg, gid)


def focus_picked_game(cfg: dict) -> PickedGame | None:
    """
    Game để log / theo dõi phiên.
    force_game_id → luôn game ép; không ép: đủ ngưỡng → hũ cao nhất >= min;
    dưới ngưỡng → hũ cao nhất mọi game (sau nổ không giữ game cũ).
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
    forced = force_game_id(cfg)
    if forced is not None:
        return [forced]
    acfg = _auto_bet_cfg(cfg)
    watch = acfg.get("game_ids")
    if isinstance(watch, list) and watch:
        return [int(x) for x in watch]
    return list(DEFAULT_JACKPOT_GAME_IDS)


def watch_game_ids_frozen(cfg: dict) -> frozenset[int]:
    """Danh sách game_id WS subscribe / theo dõi hũ."""
    return frozenset(_watch_game_ids(cfg))


def highest_jackpot_game(cfg: dict) -> PickedGame | None:
    """Hũ cao nhất trong file (không lọc min) — dùng log khi chưa đủ ngưỡng."""
    forced = forced_picked_game(cfg)
    if forced is not None:
        return forced
    return _pick_from_store(cfg, min_jp=0.0)


def pick_best_jackpot_game(cfg: dict) -> PickedGame | None:
    """
    force_game_id → chỉ game ép nếu >= min_jackpot_vnd.
    Không ép: trong game_ids, chọn hũ cao nhất nếu >= min_jackpot_vnd.
    """
    acfg = _auto_bet_cfg(cfg)
    min_jp = float(acfg.get("min_jackpot_vnd") or 0)
    forced = forced_picked_game(cfg)
    if forced is not None:
        if jackpot_compare_vnd(forced.game_id, forced.money_vnd) >= min_jp:
            return forced
        return None
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
    elif force_game_id(cfg) is not None and acfg.get("enabled"):
        fg = forced_picked_game(cfg)
        if fg:
            tail = (
                f"chờ hũ ép {fg.game_name} {_fmt_ty_vnd(fg.money_vnd)} "
                f"< {_fmt_ty_vnd(min_jp)}"
            )
        else:
            tail = f"force_game_id không hợp lệ — chờ hũ ≥ {min_jp:,.0f}"
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
