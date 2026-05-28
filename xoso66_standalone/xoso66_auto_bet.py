# -*- coding: utf-8 -*-
"""
Auto cược mini-game (LC79-style):

  - Mỗi BẮT ĐẦU PHIÊN: đọc hũ → giữ/đổi game đang chơi → chỉ chia cược đúng game đó
  - Nhận phiên mới: gán acc ngay; sau bet_plan_after_sec in BẮT ĐẦU PHIÊN + kế hoạch
  - Lệnh 1 lúc bet_place_after_sec (15s), lệnh 2 +1s, lệnh 3 +2s, … (từ đầu phiên)
  - WS kết quả → tính thắng/thua (theo pending issue, kể cả sau khi đổi game)
"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import Any

from xoso66_bet_assign import (
    BetSlot,
    assign_match_mode,
    assign_session_bets,
    verify_side_totals,
)
from xoso66_config_util import load_config
from xoso66_jackpot_picker import (
    PickedGame,
    focus_game_id,
    focus_picked_game,
    highest_jackpot_game,
    jackpot_money_for_game,
    last_watch_game_id,
    log_jackpot_below_min,
    min_jackpot_vnd,
    pick_best_jackpot_game,
    picked_game_from_id,
    resolve_side_total_vnd,
)
from xoso66_minigame_bet import BetRequest, BetResult, place_bet
from xoso66_minigame_catalog import GAME_ID_LABELS, game_by_id
from xoso66_minigame_ws import (
    register_round_result_handler,
    register_round_start_handler,
)
from xoso66_bet_plan_log import log_and_maybe_simulate_place
from xoso66_ws_balance import log_dice_bet, log_round_settlements, resolve_winning_side
from xoso66_telegram_notify import notify_auto_bet


def _auto_bet_cfg(cfg: dict) -> dict:
    raw = cfg.get("auto_bet")
    return raw if isinstance(raw, dict) else {}


def assign_bets_enabled(cfg: dict | None = None) -> bool:
    """Gán acc + in kế hoạch cược + HTTP (place_orders). Tắt = chỉ chọn game + chờ BẮT ĐẦU PHIÊN."""
    if cfg is None:
        cfg = load_config()
    return bool(_auto_bet_cfg(cfg).get("assign_bets_enabled", False))


def _bet_place_base_sec(acfg: dict) -> float:
    """Giây từ đầu phiên (t0) tới lệnh placeOrder đầu tiên."""
    return float(acfg.get("bet_place_after_sec") or 15)


def _seconds_until_bet_close(end_time: str) -> float | None:
    """Giây còn lại tới end_time (next_info.end_time) — None nếu không parse được."""
    if not end_time:
        return None
    try:
        end = datetime.strptime(str(end_time).strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return (end - datetime.now()).total_seconds()


def _plan_delay_for_round(acfg: dict, next_info: dict[str, Any]) -> tuple[float, str | None]:
    """
    bet_plan_after_sec nhưng không vượt cửa cược (next_info.end_time).
    Trả (delay_sec, skip_reason).
    """
    plan_after = float(
        acfg.get("bet_plan_after_sec")
        or acfg.get("round_start_log_delay_sec")
        or 8
    )
    rem = _seconds_until_bet_close(str(next_info.get("end_time") or ""))
    if rem is None:
        return plan_after, None
    need = plan_after + float(acfg.get("bet_place_after_sec") or 15) + 2.0
    if rem < need:
        if rem < 3.0:
            return 0.0, f"hết cửa cược (còn {rem:.0f}s, cần ~{need:.0f}s)"
        plan_after = max(0.5, rem - float(acfg.get("bet_place_after_sec") or 15) - 1.5)
    return plan_after, None


def _bet_stagger_per_user_sec(acfg: dict) -> float:
    """Khoảng cách giữa các lệnh: lệnh i tại t0 + base + i * stagger."""
    if acfg.get("bet_stagger_per_user_sec") is not None:
        return max(0.0, float(acfg.get("bet_stagger_per_user_sec") or 0))
    lo = float(acfg.get("bet_stagger_min_sec") or 1)
    hi = float(acfg.get("bet_stagger_max_sec") or lo)
    return max(0.0, lo if lo == hi else (lo + hi) / 2)


def _bet_place_at_monotonic(t0: float, acfg: dict, order_index: int) -> float:
    return t0 + _bet_place_base_sec(acfg) + order_index * _bet_stagger_per_user_sec(acfg)


def _round_start_log_delay_sec(cfg: dict) -> float:
    gw = cfg.get("game_worker") if isinstance(cfg.get("game_worker"), dict) else {}
    ab = _auto_bet_cfg(cfg)
    if isinstance(gw, dict) and "round_start_log_delay_sec" in gw:
        return max(0.0, float(gw.get("round_start_log_delay_sec") or 0))
    if "round_start_log_delay_sec" in ab:
        return max(0.0, float(ab.get("round_start_log_delay_sec") or 0))
    return 0.0


_bootstrap_lock = threading.Lock()
_bootstrap_done = False


def announce_playing_game(
    picked: PickedGame, *, cfg: dict | None = None, source: str = ""
) -> None:
    """In rõ game đang chơi / theo dõi (luôn hiện console)."""
    if cfg is None:
        cfg = load_config()
    tag = f" [{source}]" if source else ""
    best = pick_best_jackpot_game(cfg)
    min_jp = float(_auto_bet_cfg(cfg).get("min_jackpot_vnd") or 0)
    if best is not None and int(best.game_id) == int(picked.game_id):
        print(
            f"[AUTO-BET] ▶ Chơi: {picked.game_name} (id={picked.game_id}) "
            f"| hũ={picked.money_vnd:,.0f}{tag}",
            flush=True,
        )
    else:
        print(
            f"[AUTO-BET] ▶ Theo dõi phiên: {picked.game_name} (id={picked.game_id}) "
            f"| hũ={picked.money_vnd:,.0f}{tag}",
            flush=True,
        )
        if min_jp > 0:
            log_jackpot_below_min(cfg, prefix="[AUTO-BET]")
    key = (int(picked.game_id), float(picked.money_vnd))
    get_auto_bet_controller()._note_picked_key(key)


def _pick_playing_game(cfg: dict, *, wait_sec: float = 0) -> PickedGame | None:
    """Chọn game từ file hũ / playing_game.json (có thể chờ WS ghi hũ tối đa wait_sec)."""
    from xoso66_playing_game_store import load_playing_game
    from xoso66_shutdown import stopping

    acfg = _auto_bet_cfg(cfg)
    max_age = float(acfg.get("playing_game_max_age_sec") or 1800)
    deadline = time.monotonic() + max(0.0, wait_sec)
    picked: PickedGame | None = None

    picked = pick_best_jackpot_game(cfg)
    if picked is None:
        picked = highest_jackpot_game(cfg)

    saved = load_playing_game(max_age_sec=max_age)
    if picked is None and saved:
        try:
            gid = int(saved["game_id"])
            gkey, gmeta = game_by_id(gid)
            store_money = jackpot_money_for_game(cfg, gid)
            picked = PickedGame(
                game_id=gid,
                game_key=str(saved.get("game_key") or gkey),
                game_name=str(saved.get("game_name") or gmeta.get("name") or gid),
                money_vnd=float(store_money or saved.get("money_vnd") or 0),
            )
        except (KeyError, TypeError, ValueError):
            picked = None

    while picked is None and time.monotonic() < deadline and not stopping():
        picked = pick_best_jackpot_game(cfg)
        if picked is None:
            picked = highest_jackpot_game(cfg)
        if picked:
            break
        time.sleep(0.5)
    return picked


def init_playing_game(
    cfg: dict | None = None,
    *,
    source: str = "khởi động",
    wait_sec: float = 0,
    announce: bool = True,
) -> bool:
    """
    Gán game đang chơi sớm (trước / song song WS) để không bỏ lỡ phiên đầu.
    Trả True nếu đã có active_game_id.
    """
    if cfg is None:
        cfg = load_config()
    if not _auto_bet_cfg(cfg).get("enabled"):
        return False

    from xoso66_playing_game_store import save_playing_game

    ctrl = get_auto_bet_controller()
    with ctrl._lock:
        cur = ctrl._active_game_id

    picked = _pick_playing_game(cfg, wait_sec=wait_sec)
    if not picked:
        watch_id = last_watch_game_id(cfg)
        if watch_id is not None:
            picked = picked_game_from_id(cfg, watch_id)
    if not picked:
        if cur is None and float(_auto_bet_cfg(cfg).get("min_jackpot_vnd") or 0) > 0:
            log_jackpot_below_min(cfg, prefix="[AUTO-BET]")
        return cur is not None

    if cur is not None and int(cur) == int(picked.game_id):
        return True

    if announce:
        ctrl.apply_playing_game(picked, cfg=cfg, source=source)
    else:
        ctrl._set_playing_game(picked)
    save_playing_game(
        game_id=picked.game_id,
        game_key=picked.game_key,
        game_name=picked.game_name,
        money_vnd=picked.money_vnd,
    )
    if source in ("khởi động", "bootstrap"):
        print(
            f"[AUTO-BET] Chờ WS báo BẮT ĐẦU PHIÊN — {picked.game_name} "
            f"(thường vài giây–≤1 phút, tùy lúc kết nối WS)",
            flush=True,
        )
    return True


def try_bootstrap_playing_game(cfg: dict | None = None) -> bool:
    """
    Sau khi WS pool chạy: chọn game nếu chưa có (chờ file hũ tối đa bootstrap_pick_sec).
    Trả True nếu vừa gán / đã có active_game_id.
    """
    global _bootstrap_done
    with _bootstrap_lock:
        if _bootstrap_done:
            return False
    if cfg is None:
        cfg = load_config()
    acfg = _auto_bet_cfg(cfg)
    if not acfg.get("enabled"):
        with _bootstrap_lock:
            _bootstrap_done = True
        return False

    wait_sec = float(acfg.get("bootstrap_pick_sec") or 30)
    ok = init_playing_game(cfg, source="bootstrap", wait_sec=wait_sec)

    with _bootstrap_lock:
        _bootstrap_done = True

    if not ok:
        log_jackpot_below_min(cfg, prefix="[AUTO-BET] Bootstrap")
        print(
            "[AUTO-BET] Bootstrap: theo dõi phiên game cuối khi WS báo BẮT ĐẦU",
            flush=True,
        )
    return ok


def _sleep_until(deadline: float) -> bool:
    from xoso66_shutdown import stopping

    while time.monotonic() < deadline:
        if stopping():
            return False
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    return True


def _assign_session_bets_timed(
    cfg: dict,
    timeout_sec: float,
    *,
    jackpot_vnd: float | None = None,
) -> tuple[list[BetSlot], str, str]:
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(assign_session_bets, cfg, jackpot_vnd=jackpot_vnd)
        try:
            return fut.result(timeout=max(1.0, timeout_sec))
        except FuturesTimeout:
            return [], f"gán acc/quota quá {timeout_sec:.0f}s", ""


def _quick_ping_one_for_bet(
    account_id: str,
    username: str,
    *,
    game_key: str,
    cfg: dict | None = None,
) -> tuple[bool, dict[str, Any] | None, str]:
    """Ping nhanh (~1–3s/acc); fail → refresh user-token (gameurl → đầy đủ) rồi ping lại."""
    from xoso66_minigame_catalog import game_by_key
    from xoso66_minigame_http import get_minigame
    from xoso66_minigame_refresh import (
        ensure_user_token_for_bet,
        ping_user_token,
        refresh_minigame_cf,
    )
    from xoso66_session import ensure_session, persist_session

    aid = str(account_id).strip()
    user = str(username or "?").strip()
    g = game_by_key(game_key)
    gid = int(g["game_id"])
    gname = str(g.get("gamename") or "lobby")
    sub = str(g.get("sub_game_code") or "")
    acfg = _auto_bet_cfg(cfg or {})
    auto_on_fail = bool(acfg.get("token_refresh_auto_on_fail", True))
    allow_slow = bool(acfg.get("token_refresh_playwright_on_bet", True))

    try:
        session = ensure_session(aid, force_login=False)
        mg = get_minigame(session)
        if not (mg.get("cookies") or {}).get("cf_clearance"):
            refresh_minigame_cf(
                session,
                game_id=gid,
                gamename=gname,
                allow_playwright=False,
                timeout=5,
            )
        ping = ping_user_token(
            session,
            game_id=gid,
            gamename=gname,
            sub_game_code=sub or None,
        )
        if ping.get("ok"):
            return True, session, "ping OK"
        code = ping.get("code")
        msg = ping.get("msg") or ping.get("reason") or "ping fail"
        if not auto_on_fail:
            return False, None, f"ping code={code} {msg}"
        print(
            f"  ↻ Token {user}: ping code={code} {msg} — refresh user-token…",
            flush=True,
        )
        ok, refresh_msg = ensure_user_token_for_bet(
            session,
            aid,
            game_key=game_key,
            allow_slow_refresh=allow_slow,
            auto_refresh_on_fail=auto_on_fail,
        )
        persist_session(aid, session)
        if ok:
            return True, session, f"ping OK sau refresh ({refresh_msg})"
        return False, None, f"ping code={code} {msg}; refresh: {refresh_msg}"
    except Exception as e:
        return False, None, str(e)


def _validate_round_tokens_all_or_nothing(
    slots: list[BetSlot],
    *,
    game_key: str,
    cfg: dict,
    timeout_sec: float,
) -> tuple[dict[str, dict[str, Any]], str]:
    """
    Ping song song mọi acc gán trong phiên.
    Một acc fail / quá timeout → hủy cược cả phiên (không đặt acc nào).
    """
    from concurrent.futures import as_completed

    unique: dict[str, str] = {}
    for s in slots:
        aid = str(s.account_id).strip()
        if aid:
            unique[aid] = s.username

    if not unique:
        return {}, "không có acc để ping"

    acfg = _auto_bet_cfg(cfg)
    workers = min(
        int(acfg.get("token_check_parallel") or 8),
        max(1, len(unique)),
    )
    deadline = time.monotonic() + max(2.0, float(timeout_sec))
    sessions: dict[str, dict[str, Any]] = {}
    invalid: list[tuple[str, str, str]] = []
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(
                _quick_ping_one_for_bet,
                aid,
                unique[aid],
                game_key=game_key,
                cfg=cfg,
            ): aid
            for aid in unique
        }
        try:
            for fut in as_completed(
                futs, timeout=max(0.1, deadline - time.monotonic())
            ):
                aid = futs[fut]
                user = unique[aid]
                try:
                    ok, session, msg = fut.result()
                except Exception as e:
                    ok, session, msg = False, None, str(e)
                if ok and session is not None:
                    with lock:
                        sessions[aid] = session
                else:
                    with lock:
                        invalid.append((aid, user, msg))
        except FuturesTimeout:
            pending = [unique[futs[f]] for f in futs if not f.done()]
            names = ", ".join(pending[:8])
            extra = f"… +{len(pending) - 8}" if len(pending) > 8 else ""
            return (
                {},
                f"ping token quá {timeout_sec:.0f}s ({names}{extra}) — hủy cược cả phiên",
            )

    if invalid:
        for _aid, u, msg in invalid:
            print(f"  !! Token {u or '?'}: {msg}", flush=True)
        return (
            {},
            f"{len(invalid)}/{len(unique)} acc ping fail — hủy cược cả phiên",
        )
    if len(sessions) != len(unique):
        return {}, "thiếu acc ping OK — hủy cược cả phiên"
    return sessions, ""


def _sessions_for_slots(slots: list[BetSlot]) -> dict[str, dict[str, Any]]:
    """Session từ DB (không gọi ensure_session / getBalance — tránh treo SOCKS)."""
    import copy

    from xoso66_sessions_io import load_sessions

    accounts = load_sessions()
    out: dict[str, dict[str, Any]] = {}
    for s in slots:
        base = accounts.get(s.account_id)
        if not base:
            print(f"  !! Session {s.username}: không có trong DB", flush=True)
            continue
        mg = (base.get("minigame") or {}) if isinstance(base.get("minigame"), dict) else {}
        if not str(mg.get("user_token") or "").strip():
            print(f"  !! Session {s.username}: thiếu minigame.user_token", flush=True)
            continue
        out[s.account_id] = copy.deepcopy(base)
    return out


def _token_check_before_bet_enabled(acfg: dict, *, place_orders: bool = False) -> bool:
    """Khi place_orders=true luôn bắt buộc ping — không đặt nếu fail."""
    if place_orders:
        return True
    return bool(acfg.get("token_check_before_bet", True))


def _place_bet_timed(
    session: dict,
    req: BetRequest,
    *,
    account_id: str,
    http_timeout: int,
    wall_timeout: float,
    check_token: bool = True,
) -> BetResult:
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(
            place_bet,
            session,
            req,
            account_id=account_id,
            check_token=check_token,
            http_timeout=http_timeout,
        )
        try:
            return fut.result(timeout=max(3.0, wall_timeout))
        except FuturesTimeout:
            return BetResult(
                ok=False,
                game_key=req.game_key,
                game_id=0,
                side=req.side,
                play_id=0,
                amount=int(req.amount),
                http_status=0,
                code=0,
                msg=f"placeOrder quá {wall_timeout:.0f}s (proxy/HTTP treo)",
            )


def _place_bet_for_slot(
    session: dict,
    req: BetRequest,
    *,
    account_id: str,
    username: str,
    game_key: str,
    cfg: dict,
    http_timeout: int,
    wall_timeout: float,
    tokens_prevalidated: bool = False,
) -> BetResult:
    """Đặt cược; token đã ping cả phiên trước đó (all-or-nothing)."""
    from xoso66_minigame_refresh import (
        ensure_user_token_for_bet,
        is_minigame_session_error,
    )
    from xoso66_session import persist_session

    acfg = _auto_bet_cfg(cfg)
    allow_slow = bool(acfg.get("token_refresh_playwright_on_bet", True))
    auto_on_fail = bool(acfg.get("token_refresh_auto_on_fail", True))

    if not tokens_prevalidated and _token_check_before_bet_enabled(
        acfg, place_orders=True
    ):
        ok, msg = ensure_user_token_for_bet(
            session,
            account_id,
            game_key=game_key,
            allow_slow_refresh=allow_slow,
            auto_refresh_on_fail=auto_on_fail,
        )
        if not ok:
            print(f"  !! Token {username}: {msg}", flush=True)
            return BetResult(
                ok=False,
                game_key=req.game_key,
                game_id=0,
                side=req.side,
                play_id=0,
                amount=int(req.amount),
                http_status=0,
                code=0,
                msg=msg,
            )

    rep = _place_bet_timed(
        session,
        req,
        account_id=account_id,
        http_timeout=http_timeout,
        wall_timeout=wall_timeout,
        check_token=False,
    )
    if rep.ok or not is_minigame_session_error(rep.code, rep.msg):
        return rep

    ok2, msg2 = ensure_user_token_for_bet(
        session,
        account_id,
        game_key=game_key,
        allow_slow_refresh=allow_slow,
        auto_refresh_on_fail=auto_on_fail,
    )
    if account_id:
        persist_session(account_id, session)
    if not ok2:
        return rep
    print(f"  ↻ {username}: refresh token → thử lại placeOrder", flush=True)
    return _place_bet_timed(
        session,
        req,
        account_id=account_id,
        http_timeout=http_timeout,
        wall_timeout=wall_timeout,
        check_token=False,
    )


def _validate_slot_tokens_timed(
    slots: list[BetSlot],
    *,
    game_key: str,
    cfg: dict,
    timeout_sec: float,
) -> tuple[dict[str, dict[str, Any]], str]:
    """All-or-nothing — trả (sessions, lỗi); lỗi khác rỗng = OK đặt cả phiên."""
    return _validate_round_tokens_all_or_nothing(
        slots,
        game_key=game_key,
        cfg=cfg,
        timeout_sec=timeout_sec,
    )


class AutoBetController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_game_id: int | None = None
        self._active_game_key: str = ""
        self._active_jackpot: float = 0.0
        self._session_seen: set[str] = set()
        self._pending_bets: dict[str, list[BetSlot]] = {}
        self._last_picked_key: tuple[int, float] | None = None
        self._last_gate_log_key: tuple[int, str] | None = None

    def _bet_key(self, game_id: int, issue: str) -> str:
        return f"{int(game_id)}:{str(issue).strip()}"

    def _note_picked_key(self, key: tuple[int, float]) -> None:
        with self._lock:
            self._last_picked_key = key

    def _set_playing_game(self, picked: PickedGame | None) -> None:
        with self._lock:
            if picked is None:
                self._active_game_id = None
                self._active_game_key = ""
                self._active_jackpot = 0.0
                return
            self._active_game_id = int(picked.game_id)
            self._active_game_key = str(picked.game_key)
            self._active_jackpot = float(picked.money_vnd)

    def apply_playing_game(
        self, picked: PickedGame, *, cfg: dict, source: str = "phiên"
    ) -> None:
        self._set_playing_game(picked)
        key = (int(picked.game_id), float(picked.money_vnd))
        with self._lock:
            if key == self._last_picked_key:
                return
        announce_playing_game(picked, cfg=cfg, source=source)
        try:
            from xoso66_playing_game_store import save_playing_game

            save_playing_game(
                game_id=picked.game_id,
                game_key=picked.game_key,
                game_name=picked.game_name,
                money_vnd=picked.money_vnd,
            )
        except Exception:
            pass

    def _log_playing_game(self, picked: PickedGame, *, cfg: dict) -> None:
        acfg = _auto_bet_cfg(cfg)
        if not acfg.get("log_jackpot_pick", True):
            return
        self.apply_playing_game(picked, cfg=cfg, source="phiên")

    def active_game_id(self) -> int | None:
        with self._lock:
            return self._active_game_id

    def active_game_key(self) -> str:
        with self._lock:
            return str(self._active_game_key or "").strip()

    def _log_watch_round_start(
        self, game_id: int, issue: str, *, cfg: dict
    ) -> None:
        """BẮT ĐẦU PHIÊN (chỉ theo dõi) khi chưa đủ hũ — cùng format WS."""
        try:
            _, gmeta = game_by_id(int(game_id))
            game_label = str(
                gmeta.get("name") or GAME_ID_LABELS.get(int(game_id), game_id)
            )
        except KeyError:
            game_label = str(GAME_ID_LABELS.get(int(game_id), game_id))
        jp = jackpot_money_for_game(cfg, int(game_id))
        plan_after = float(
            _auto_bet_cfg(cfg).get("bet_plan_after_sec")
            or _round_start_log_delay_sec(cfg)
            or 8
        )
        from xoso66_round_log import log_round_start_line, round_console_lock

        min_jp = min_jackpot_vnd(cfg)

        def _emit() -> None:
            with round_console_lock():
                log_round_start_line(
                    game_label=game_label,
                    jackpot_vnd=jp,
                    issue=issue,
                    min_jackpot_vnd=min_jp if min_jp > 0 else None,
                )

        if plan_after > 0:
            threading.Timer(plan_after, _emit).start()
        else:
            _emit()

    def resolve_round_start(
        self, cfg: dict, event_game_id: int, *, issue: str = ""
    ) -> tuple[bool, str]:
        """
        Trước chia tiền: so hũ, cập nhật game đang chơi.
        Trả (should_bet, reason_skip).
        """
        best = pick_best_jackpot_game(cfg)
        gid = int(event_game_id)
        issue_s = str(issue or "").strip()

        with self._lock:
            playing = self._active_game_id

        if best is None:
            top = highest_jackpot_game(cfg)
            watch_id = int(top.game_id) if top else last_watch_game_id(cfg)
            if watch_id is None or gid != int(watch_id):
                return False, ""
            watch = top or picked_game_from_id(cfg, int(watch_id))
            if watch is not None:
                self._set_playing_game(watch)
                try:
                    from xoso66_playing_game_store import save_playing_game

                    save_playing_game(
                        game_id=watch.game_id,
                        game_key=watch.game_key,
                        game_name=watch.game_name,
                        money_vnd=watch.money_vnd,
                    )
                except Exception:
                    pass
            gate_key = (gid, issue_s) if issue_s else None
            with self._lock:
                if gate_key and self._last_gate_log_key == gate_key:
                    return False, "below_min_jackpot"
                if gate_key:
                    self._last_gate_log_key = gate_key
            return False, "below_min_jackpot"

        if playing is None:
            self._set_playing_game(best)
            self._log_playing_game(best, cfg=cfg)
            playing = best.game_id

        if gid != int(playing):
            return False, ""

        if int(best.game_id) != int(playing):
            self._set_playing_game(best)
            print(
                f"[AUTO-BET] Hũ đổi → {best.game_name} (id={best.game_id}, "
                f"hũ={best.money_vnd:,.0f}); chờ BẮT ĐẦU PHIÊN game đó",
                flush=True,
            )
            return False, ""

        self._set_playing_game(best)
        with self._lock:
            self._last_gate_log_key = None
        return True, ""

    def _skip_round_assign_fail(
        self,
        *,
        game_label: str,
        issue: str,
        reason: str,
        cfg: dict,
    ) -> None:
        """
        Hủy phiên: chia mức/gán acc fail, dư mức, không pool WS, ping token…
        Không gửi Telegram (chỉ log console).
        """
        print(
            f"- Game {game_label} issue={issue}: bỏ phiên — {reason}",
            flush=True,
        )

    def _handle_round_start_worker(
        self,
        game_id: int,
        issue: str,
        next_info: dict[str, Any],
        *,
        reporter: str = "",
    ) -> None:
        import traceback

        from xoso66_shutdown import stopping

        try:
            self._handle_round_start_worker_impl(
                game_id, issue, next_info, reporter=reporter
            )
        except Exception as e:
            print(
                f"[AUTO-BET] Lỗi worker issue={issue}: {e}\n"
                f"{traceback.format_exc()}",
                flush=True,
            )

    def _handle_round_start_worker_impl(
        self,
        game_id: int,
        issue: str,
        next_info: dict[str, Any],
        *,
        reporter: str = "",
    ) -> None:
        from xoso66_shutdown import stopping

        t0 = time.monotonic()
        cfg = load_config()
        acfg = _auto_bet_cfg(cfg)
        plan_deadline = float(acfg.get("plan_deadline_sec") or 10)
        plan_after, skip_reason = _plan_delay_for_round(acfg, next_info)
        if skip_reason:
            self._skip_round_assign_fail(
                game_label=str(GAME_ID_LABELS.get(int(game_id), game_id)),
                issue=issue,
                reason=skip_reason,
                cfg=cfg,
            )
            return

        with self._lock:
            target = self._active_game_id
            jackpot = self._active_jackpot
            gkey = self._active_game_key
        if target is None or int(game_id) != int(target):
            return

        key = self._bet_key(game_id, issue)
        with self._lock:
            if key in self._session_seen:
                return
            self._session_seen.add(key)
            if len(self._session_seen) > 500:
                self._session_seen.clear()

        try:
            _, gmeta = game_by_id(int(game_id))
            game_label = str(gmeta.get("name") or GAME_ID_LABELS.get(int(game_id), game_id))
        except KeyError:
            game_label = str(GAME_ID_LABELS.get(int(game_id), game_id))

        if stopping():
            return

        with self._lock:
            jp_header = float(self._active_jackpot or 0)
        side_total = resolve_side_total_vnd(cfg, jp_header)

        slots, err, partial_note = _assign_session_bets_timed(
            cfg, plan_deadline, jackpot_vnd=jp_header
        )
        elapsed = time.monotonic() - t0
        if elapsed > plan_deadline:
            print(
                f"[AUTO-BET] Gán acc {elapsed:.1f}s (>{plan_deadline:.0f}s) — "
                f"vẫn xử lý nếu gán OK",
                flush=True,
            )
        if err:
            self._skip_round_assign_fail(
                game_label=game_label,
                issue=issue,
                reason=err,
                cfg=cfg,
            )
            return

        if assign_match_mode(acfg) == 1:
            tot_err = verify_side_totals(slots, side_total)
            if tot_err:
                self._skip_round_assign_fail(
                    game_label=game_label,
                    issue=issue,
                    reason=tot_err,
                    cfg=cfg,
                )
                return
        elif partial_note:
            print(
                f"- Game {game_label} issue={issue}: cược một phần — {partial_note}",
                flush=True,
            )

        with self._lock:
            self._pending_bets[key] = list(slots)

        place_orders = bool(acfg.get("place_orders", False))
        sessions: dict[str, dict[str, Any]] = {}
        tokens_prevalidated = False
        if place_orders:
            if _token_check_before_bet_enabled(acfg, place_orders=True):
                tok_timeout = float(acfg.get("token_validate_timeout_sec") or 12)
                sessions, tok_err = _validate_slot_tokens_timed(
                    slots,
                    game_key=gkey,
                    cfg=cfg,
                    timeout_sec=tok_timeout,
                )
                if tok_err:
                    with self._lock:
                        self._pending_bets.pop(key, None)
                    self._skip_round_assign_fail(
                        game_label=game_label,
                        issue=issue,
                        reason=f"đã gán acc nhưng không đặt — {tok_err}",
                        cfg=cfg,
                    )
                    return
                tokens_prevalidated = True
            else:
                sessions = _sessions_for_slots(slots)
                slots = [s for s in slots if s.account_id in sessions]
                with self._lock:
                    self._pending_bets[key] = list(slots)
                if not slots:
                    print(
                        f"- Game {game_label} issue={issue}: "
                        f"đã gán acc nhưng không đặt — không load session",
                        flush=True,
                    )
                    with self._lock:
                        self._pending_bets.pop(key, None)
                    return

        from xoso66_round_log import (
            log_round_bet_footer,
            log_round_start_line,
            round_console_lock,
        )

        with round_console_lock():
            if plan_after > 0 and not _sleep_until(t0 + plan_after):
                return

            log_round_start_line(
                game_label=game_label,
                jackpot_vnd=jp_header,
                issue=str(issue).strip(),
            )
            log_and_maybe_simulate_place(slots, cfg)

            if not place_orders:
                return

            if stopping():
                return

            ok_n = 0
            fail_n = 0
            price_scale = float(acfg.get("order_price_scale") or 1)
            order_slots = list(slots)
            random.shuffle(order_slots)
            http_timeout = int(acfg.get("place_order_timeout_sec") or 20)

            for i, slot in enumerate(order_slots):
                if stopping():
                    break
                if not _sleep_until(_bet_place_at_monotonic(t0, acfg, i)):
                    if i == 0:
                        print(
                            f"  [{issue}] !! Dừng trước khi gửi placeOrder",
                            flush=True,
                        )
                    break

                session = sessions.get(slot.account_id)
                if not session:
                    fail_n += 1
                    print(f"  !! {slot.username}: thiếu session", flush=True)
                    continue

                price = int(slot.amount_vnd * price_scale)
                rep = _place_bet_for_slot(
                    session,
                    BetRequest(
                        game_key=gkey,
                        side=slot.side,
                        amount=price,
                        issue=str(issue),
                    ),
                    account_id=slot.account_id,
                    username=slot.username,
                    game_key=gkey,
                    cfg=cfg,
                    http_timeout=http_timeout,
                    wall_timeout=float(
                        acfg.get("place_order_wall_timeout_sec") or 25
                    ),
                    tokens_prevalidated=tokens_prevalidated,
                )
                if rep.ok:
                    ok_n += 1
                    bal = rep.balance
                    if bal is None:
                        from xoso66_accounts_db import get_account

                        row = get_account(slot.account_id) or {}
                        try:
                            bal = float(row.get("balance") or 0)
                        except (TypeError, ValueError):
                            bal = 0.0
                    log_dice_bet(
                        slot.username,
                        side=slot.side,
                        amount_vnd=price,
                        balance=bal or 0,
                        issue=str(rep.issue or issue),
                    )
                else:
                    fail_n += 1
                    print(
                        f"  !! {slot.username} {_side_abbr(slot.side)} "
                        f"{slot.amount_vnd:,} FAIL — {rep.msg}",
                        flush=True,
                    )

            log_round_bet_footer(
                issue=str(issue).strip(),
                ok_n=ok_n,
                total=len(slots),
                fail_n=fail_n,
            )
            if fail_n > 0:
                notify_auto_bet(
                    f"ĐẶT CƯỢC THẤT BẠI — {game_label} issue={issue}\n"
                    f"OK {ok_n}/{len(slots)} FAIL {fail_n} — kiểm tra log",
                    cfg=cfg,
                )

    def on_round_start(
        self,
        game_id: int,
        issue: str,
        next_info: dict[str, Any],
        *,
        reporter: str = "",
    ) -> None:
        cfg = load_config()
        acfg = _auto_bet_cfg(cfg)
        if not acfg.get("enabled"):
            return

        from xoso66_shutdown import stopping

        if stopping():
            return

        # IMPORTANT:
        # - Nếu đã có game đang chơi (active_game_id) thì ưu tiên xử lý đúng game đó,
        #   không phụ thuộc focus_game_id (hũ cao nhất) vì focus có thể nhảy khi nhiều game vượt ngưỡng.
        # - Nếu chưa có active_game_id thì mới dùng focus_game_id để tránh xử lý spam mọi game.
        with self._lock:
            active_gid = self._active_game_id
        if active_gid is not None:
            if int(game_id) != int(active_gid):
                return
        else:
            focus_gid = focus_game_id(cfg)
            if focus_gid is None:
                init_playing_game(cfg, source="phiên", wait_sec=0, announce=True)
                focus_gid = focus_game_id(cfg)
            if focus_gid is None:
                return
            if int(game_id) != int(focus_gid):
                return
        picked_focus = focus_picked_game(cfg)
        if picked_focus is not None:
            with self._lock:
                stale = self._active_game_id
            if stale is None or int(stale) != int(picked_focus.game_id):
                self._set_playing_game(picked_focus)

        issue_s = str(issue or "").strip()
        should_bet, skip_reason = self.resolve_round_start(
            cfg, int(game_id), issue=issue_s
        )
        if not should_bet:
            if skip_reason == "below_min_jackpot" and assign_bets_enabled(cfg):
                self._log_watch_round_start(int(game_id), issue_s, cfg=cfg)
            return

        if not assign_bets_enabled(cfg):
            return

        t = threading.Thread(
            target=self._handle_round_start_worker,
            args=(game_id, issue, next_info),
            kwargs={"reporter": reporter},
            name=f"xoso66-bet-{game_id}-{issue}",
            daemon=True,
        )
        t.start()

    def on_round_result(
        self,
        game_id: int,
        issue: str,
        open_data: dict[str, Any],
        *,
        reporter: str = "",
    ) -> None:
        cfg = load_config()
        acfg = _auto_bet_cfg(cfg)
        if not acfg.get("enabled") or not assign_bets_enabled(cfg):
            return

        key = self._bet_key(game_id, issue)
        with self._lock:
            slots = list(self._pending_bets.pop(key, []))
        if not slots:
            return

        try:
            from xoso66_jackpot_hit_notify import notify_jackpot_hit_for_our_bets
            from xoso66_minigame_catalog import GAME_ID_LABELS

            notify_jackpot_hit_for_our_bets(
                game_id,
                issue,
                open_data,
                slots,
                game_label=GAME_ID_LABELS.get(int(game_id), ""),
                cfg=cfg,
            )
        except Exception as e:
            print(f"[JACKPOT-HIT] {e}", flush=True)

        if not resolve_winning_side(open_data):
            return

        win_rate = float(acfg.get("win_payout_rate") or 0.98)
        log_round_settlements(
            slots,
            open_data,
            win_rate=win_rate,
            issue=issue,
        )
        try:
            from xoso66_ws_pool import prune_ws_after_settlement

            prune_ws_after_settlement(
                cfg, [s.account_id for s in slots]
            )
        except Exception as e:
            print(f"[WS-POOL] Sau KQ — không prune WS: {e}", flush=True)


def pending_bet_account_ids() -> set[str]:
    """Acc còn lệnh phiên chưa có KQ — không ngắt WS đầu phiên kế."""
    with _controller._lock:
        out: set[str] = set()
        for slots in _controller._pending_bets.values():
            for s in slots:
                aid = str(s.account_id).strip()
                if aid:
                    out.add(aid)
        return out


def _side_abbr(side: str) -> str:
    return "T" if str(side).lower() in ("tai", "tài", "big") else "X"


_controller = AutoBetController()


def get_auto_bet_controller() -> AutoBetController:
    return _controller


def _round_start_callback(
    game_id: int,
    issue: str,
    next_info: dict[str, Any],
    *,
    reporter: str = "",
) -> None:
    try:
        _controller.on_round_start(game_id, issue, next_info, reporter=reporter)
    except Exception as e:
        print(f"[AUTO-BET] Lỗi phiên mới: {e}", flush=True)


def _round_result_callback(
    game_id: int,
    issue: str,
    open_data: dict[str, Any],
    *,
    reporter: str = "",
) -> None:
    try:
        _controller.on_round_result(game_id, issue, open_data, reporter=reporter)
    except Exception as e:
        print(f"[AUTO-BET] Lỗi kết quả: {e}", flush=True)


_handlers_registered = False


def setup_auto_bet_handlers() -> None:
    global _handlers_registered
    if _handlers_registered:
        return
    register_round_start_handler(_round_start_callback)
    register_round_result_handler(_round_result_callback)
    _handlers_registered = True


def auto_bet_loop() -> None:
    from xoso66_shutdown import stopping

    setup_auto_bet_handlers()

    cfg = load_config()
    if _auto_bet_cfg(cfg).get("enabled"):
        init_playing_game(cfg, source="khởi động", wait_sec=0)
    acfg = _auto_bet_cfg(cfg)
    min_jp = float(acfg.get("min_jackpot_vnd") or 0)

    if assign_bets_enabled(cfg):
        plan_d = float(acfg.get("plan_deadline_sec") or 10)
        plan_a = float(acfg.get("bet_plan_after_sec") or 8)
        po = bool(acfg.get("place_orders", False))
        print(
            f"[AUTO-BET] Bật — game hũ ≥ {min_jp:,.0f} | gán acc ≤{plan_d:.0f}s | "
            f"in kế hoạch sau {plan_a:.0f}s"
            + (" | HTTP cược" if po else " | chỉ log (place_orders=false)"),
            flush=True,
        )
    else:
        print(
            f"[AUTO-BET] Bật — chọn game (hũ ≥ {min_jp:,.0f}) → chờ BẮT ĐẦU PHIÊN",
            flush=True,
        )

    while not stopping():
        if not _auto_bet_cfg(load_config()).get("enabled"):
            time.sleep(5)
            continue
        time.sleep(1)

    print("[AUTO-BET] Đã dừng.", flush=True)


def start_auto_bet_thread() -> threading.Thread:
    t = threading.Thread(
        target=auto_bet_loop,
        name="xoso66-auto-bet",
        daemon=False,
    )
    t.start()
    return t
