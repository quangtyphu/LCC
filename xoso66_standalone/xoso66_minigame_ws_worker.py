# -*- coding: utf-8 -*-
"""
Worker WS mini-game: N account (pool balance cao, «Đang Chơi» — xoso66_ws_pool).

Nhiều acc giữ WS. Mỗi sự kiện chỉ xử lý **một lần**: acc nào nhận gói WS
trước thì báo/lưu (WsBroadcastCoordinator — giống LC79 ``session_seen`` / new-session).

  1. Lưu jackpot 5 game → data/minigame_jackpots.json
  2. BẮT ĐẦU PHIÊN (open_info → next_info, theo từng game_id)
  3. KẾT QUẢ (open_info phiên vừa xong)

Chạy riêng:
  python xoso66_minigame_ws_worker.py

Hoặc bật game_worker_enabled trong xoso66_config.json + python main.py
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
import threading
import time
from typing import Any

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from xoso66_config_util import configure_stdio_utf8

configure_stdio_utf8()

from xoso66_accounts_db import init_db, usernames_for_log
from xoso66_minigame_catalog import DEFAULT_JACKPOT_GAME_IDS, GAME_ID_LABELS
from xoso66_minigame_jackpot_store import MinigameJackpotStore
from xoso66_minigame_ws import (
    DEFAULT_WS_SUBSCRIBE,
    WsBroadcastCoordinator,
    listen_minigame_ws,
    parse_watch_game_ids,
)
from xoso66_ws_pool import select_ws_account_ids, ws_account_count

_ws_pool_round_check = threading.Event()
_ws_after_deposit_check = threading.Event()
_ws_evict_ids: set[str] = set()
_ws_evict_lock = threading.Lock()
_ws_after_deposit_ids: set[str] = set()
_ws_after_deposit_lock = threading.Lock()

WATCH_GAME_IDS = frozenset(DEFAULT_JACKPOT_GAME_IDS)
_cli_watch_override = False


def effective_watch_game_ids(cfg: dict) -> frozenset[int]:
    """CLI --watch-games ghi đè; không thì config force_game_id / game_ids."""
    if _cli_watch_override:
        return WATCH_GAME_IDS
    from xoso66_jackpot_picker import watch_game_ids_frozen

    return watch_game_ids_frozen(cfg)

_token_maintain_ids: list[str] = []
_token_maintain_lock = threading.Lock()

def _sync_ws_status_blocking(
    cfg: dict[str, Any],
    *,
    leaving: list[str],
    joining: list[str],
) -> None:
    from xoso66_ws_pool import sync_status_for_ws_pool_change

    sync_status_for_ws_pool_change(cfg, leaving=leaving, joining=joining)


def _default_ws_count() -> int:
    try:
        from xoso66_config_util import load_config

        return ws_account_count(load_config())
    except Exception:
        return 12


WS_WORKER_COUNT = int(os.environ.get("XOSO66_WS_WORKER_ACCOUNTS") or _default_ws_count())
TOKEN_MAINTAIN_INTERVAL_SEC = int(os.environ.get("XOSO66_TOKEN_MAINTAIN_SEC", "1800"))


def _set_token_maintain_ids(account_ids: list[str]) -> None:
    with _token_maintain_lock:
        _token_maintain_ids[:] = [str(x).strip() for x in account_ids if str(x).strip()]


def _log_async_task_result(task: asyncio.Task[None]) -> None:
    """Tránh 'Future exception was never retrieved' khi task nền lỗi."""
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[WS-POOL] Task {task.get_name() or '?'} lỗi: {e}", flush=True)


def _maintain_user_tokens_loop() -> None:
    """Refresh nền khi token già / ping fail — tránh hết token giữa chừng khi cược."""
    import time

    from xoso66_accounts_db import username_for_log
    from xoso66_minigame_refresh import (
        pop_urgent_token_refresh_ids,
        refresh_minigame_tokens,
        user_token_status,
    )
    from xoso66_session import ensure_session, persist_session

    from xoso66_shutdown import stopping

    def _sleep_until_maintain_due() -> None:
        from xoso66_minigame_refresh import _urgent_token_refresh_event

        for _ in range(max(1, TOKEN_MAINTAIN_INTERVAL_SEC)):
            if stopping():
                return
            if _urgent_token_refresh_event.wait(timeout=1.0):
                _urgent_token_refresh_event.clear()
                return

    while not stopping():
        _sleep_until_maintain_due()
        if stopping():
            return
        urgent = pop_urgent_token_refresh_ids()
        with _token_maintain_lock:
            pool = list(_token_maintain_ids)
        aids: list[str] = []
        seen: set[str] = set()
        for aid in list(urgent.keys()) + pool:
            a = str(aid).strip()
            if a and a not in seen:
                seen.add(a)
                aids.append(a)
        for aid in aids:
            if stopping():
                return
            try:
                from xoso66_cf import is_account_cf_rate_limited

                if is_account_cf_rate_limited(aid):
                    continue
                from xoso66_ws_pool import get_connected_ws_accounts

                from xoso66_config_util import load_config
                from xoso66_minigame_catalog import game_by_key
                from xoso66_playing_game_store import runtime_token_game_key

                cfg = load_config()
                game_key = urgent.get(aid) or runtime_token_game_key(cfg)
                g = game_by_key(game_key)
                gid = int(g["game_id"])
                gname = str(g.get("gamename") or "lobby")

                session = ensure_session(aid, force_login=False)
                st = user_token_status(session, game_id=gid, gamename=gname)
                if aid in get_connected_ws_accounts() and st.get("ping_ok") and not st.get(
                    "needs_refresh"
                ):
                    continue
                if st.get("ping_ok") and not st.get("needs_refresh"):
                    continue
                refresh_minigame_tokens(
                    session,
                    account_id=aid,
                    game_key=game_key,
                    force=not st.get("ping_ok"),
                    ws_only=bool(st.get("ping_ok")),
                )
                persist_session(aid, session)
            except Exception as e:
                print(f"[TOKEN] {username_for_log(aid)} lỗi maintain: {e}", flush=True)


async def _sleep_until_account_cf_cooldown(account_id: str, user: str) -> None:
    """Chờ hết cooldown CF — không gọi API/WS trong lúc chờ."""
    from xoso66_cf import cf_rate_limit_remaining_for_account, is_account_cf_rate_limited
    from xoso66_shutdown import stopping

    if not is_account_cf_rate_limited(account_id):
        return
    rem = int(cf_rate_limit_remaining_for_account(account_id))
    if rem <= 0:
        return
    print(
        f"⏸️ [{user}] CF rate limit — chờ {rem}s (không gọi API/WS)",
        flush=True,
    )
    while rem > 0 and not stopping():
        await asyncio.sleep(min(rem, 15))
        rem = int(cf_rate_limit_remaining_for_account(account_id))


async def run_ws_for_account(
    account_id: str,
    *,
    jackpot_store: MinigameJackpotStore,
    subscribe_spec: str,
    broadcast: WsBroadcastCoordinator,
    refresh_before_connect: bool = True,
) -> None:
    from xoso66_accounts_db import username_for_log
    from xoso66_shutdown import stopping

    aid = str(account_id).strip()
    user = username_for_log(aid)
    while not stopping():
        await _sleep_until_account_cf_cooldown(aid, user)
        if stopping():
            return
        try:
            from xoso66_config_util import load_config

            watch_ids = effective_watch_game_ids(load_config())
            await listen_minigame_ws(
                {},
                aid,
                duration_sec=0,
                game_key="taixiu_dai_loc",
                refresh_before_connect=refresh_before_connect,
                verbose=False,
                game_watch=True,
                watch_game_ids=watch_ids,
                subscribe_spec=subscribe_spec,
                subscribe_individual=True,
                ping_game_id=sorted(watch_ids),
                save_jackpot=True,
                jackpot_store=jackpot_store,
                log_game_info=False,
                broadcast_coordinator=broadcast,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            from xoso66_cf import CfRateLimitError

            if isinstance(e, CfRateLimitError):
                print(f"❌ [{user}] WS: {e}", flush=True)
                await _sleep_until_account_cf_cooldown(aid, user)
                continue
            from xoso66_ws_pool import mark_ws_connect_failed

            mark_ws_connect_failed(aid, reason=str(e)[:160], exc=e)
            print(f"❌ [{user}] WS: {e}", flush=True)
            for _ in range(5):
                if stopping():
                    return
                await asyncio.sleep(1)
        else:
            if stopping():
                break
            print(
                f"⚠️ [{user}] WS task kết thúc bất thường — chờ 20s rồi mở lại",
                flush=True,
            )
            for _ in range(20):
                if stopping():
                    return
                await asyncio.sleep(1)


class WsPoolSupervisor:
    """WS pool; ws_account_count = giới hạn slot (task + connect + pending)."""

    def __init__(self) -> None:
        self.jp_store = MinigameJackpotStore()
        self.broadcast = WsBroadcastCoordinator()
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.listener_id: str | None = None
        self.listener_task: asyncio.Task[None] | None = None
        self._listener_refresh = True
        self._token_thread: threading.Thread | None = None
        self._resync_lock = asyncio.Lock()
        self._spawn_lock = threading.Lock()
        self._connect_batch_n = 0
        self._last_respawn_at: dict[str, float] = {}

    def _sync_token_maintain_ids(self) -> None:
        ids = list(self.tasks.keys())
        if self.listener_id and self.listener_id not in ids:
            ids.append(self.listener_id)
        _set_token_maintain_ids(ids)

    async def ensure_listener(self, cfg: dict[str, Any]) -> None:
        """1 WS cố định nghe phiên/hũ — không evict cap, không cần đủ balance."""
        from xoso66_accounts_db import username_for_log
        from xoso66_shutdown import stopping
        from xoso66_ws_pool import pick_ws_listener_account, ws_listener_enabled

        if stopping():
            return
        if not ws_listener_enabled(cfg):
            return
        aid = pick_ws_listener_account(cfg)
        if not aid:
            print("[WS-LISTENER] Không có acc proxy — bỏ giữ WS nghe", flush=True)
            return
        if self.listener_id and self.listener_id != aid:
            await self._stop_listener()
        self.listener_id = aid
        if self.listener_task is not None and not self.listener_task.done():
            self._sync_token_maintain_ids()
            return
        user = username_for_log(aid)
        print(
            f"[WS-LISTENER] Giữ WS nghe phiên (không ngắt cap): {user}",
            flush=True,
        )
        refresh = self._listener_refresh
        self._listener_refresh = False
        self.listener_task = asyncio.create_task(
            run_ws_for_account(
                aid,
                jackpot_store=self.jp_store,
                subscribe_spec=DEFAULT_WS_SUBSCRIBE,
                broadcast=self.broadcast,
                refresh_before_connect=refresh,
            ),
            name=f"ws-listener-{aid}",
        )
        self.listener_task.add_done_callback(_log_async_task_result)
        self._sync_token_maintain_ids()

    async def _stop_listener(self) -> None:
        from xoso66_accounts_db import username_for_log
        from xoso66_ws_pool import unregister_ws_connected

        task = self.listener_task
        lid = str(self.listener_id or "").strip()
        self.listener_task = None
        self.listener_id = None
        if lid:
            unregister_ws_connected(lid)
        if task is None:
            return
        user = username_for_log(lid) if lid else "?"
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        print(f"[WS-LISTENER] Đã đóng WS: {user}", flush=True)

    async def restart_listener_if_dead(self, cfg: dict[str, Any]) -> None:
        from xoso66_shutdown import stopping

        if stopping():
            return
        if not self.listener_id:
            await self.ensure_listener(cfg)
            return
        task = self.listener_task
        if task is None or task.done():
            if task is not None and not task.cancelled():
                exc = task.exception()
                if exc:
                    from xoso66_accounts_db import username_for_log

                    print(
                        f"[WS-LISTENER] {username_for_log(self.listener_id)} "
                        f"rớt WS: {exc}",
                        flush=True,
                    )
            await self.ensure_listener(cfg)

    def _ensure_token_thread(self) -> None:
        if self._token_thread and self._token_thread.is_alive():
            return
        self._token_thread = threading.Thread(
            target=_maintain_user_tokens_loop,
            name="xoso66-token-maintain",
            daemon=True,
        )
        self._token_thread.start()

    def _spawn_task(self, aid: str, *, lead: str, refresh: bool) -> None:
        """Một nick chỉ một task WS; không ghi đè task đang chạy."""
        aid = str(aid).strip()
        if not aid or aid == self.listener_id:
            return
        with self._spawn_lock:
            old = self.tasks.get(aid)
            if old is not None and not old.done():
                return
            if old is not None:
                old.cancel()
            task = asyncio.create_task(
                run_ws_for_account(
                    aid,
                    jackpot_store=self.jp_store,
                    subscribe_spec=DEFAULT_WS_SUBSCRIBE,
                    broadcast=self.broadcast,
                    refresh_before_connect=refresh,
                ),
                name=f"ws-{aid}",
            )
            task.add_done_callback(_log_async_task_result)
            self.tasks[aid] = task

    def _pool_slots_busy(self) -> bool:
        """Đang mở WS (pending / batch connect) — không resync bù trùng."""
        from xoso66_ws_pool import get_pending_ws_slot_ids

        if self._connect_batch_n > 0:
            return True
        if get_pending_ws_slot_ids():
            return True
        return False

    async def _spawn_added(
        self,
        added: list[str],
        *,
        lead: str,
        refresh_new: bool,
        cfg: dict[str, Any],
    ) -> None:
        from xoso66_accounts_db import get_account, username_for_log
        from xoso66_proxy import is_proxy_dead, probe_proxy_socks, report_proxy_dead, resolve_proxy
        from xoso66_ws_pool import (
            account_balance_vnd,
            account_deposit_in_flight,
            min_balance_for_ws,
            ws_bulk_refresh_threshold,
        )

        if not added:
            return
        min_bal = min_balance_for_ws(cfg)
        ready: list[str] = []
        probe_new = len(added) <= 3
        for aid in added:
            aid = str(aid).strip()
            if not aid or aid == self.listener_id:
                continue
            if is_proxy_dead(aid):
                continue
            if account_deposit_in_flight(aid, cfg):
                print(
                    f"[WS-POOL] Bỏ mở WS {username_for_log(aid)} — đang nạp",
                    flush=True,
                )
                continue
            row = get_account(aid) or {}
            if account_balance_vnd(row) < min_bal:
                print(
                    f"[WS-POOL] Bỏ mở WS {username_for_log(aid)} — balance "
                    f"{account_balance_vnd(row):,.0f} < {min_bal:,}",
                    flush=True,
                )
                continue
            if probe_new:
                px = resolve_proxy(row)
                ok_px, px_err = await asyncio.to_thread(probe_proxy_socks, px)
                if not ok_px:
                    report_proxy_dead(
                        aid,
                        proxy_str=px,
                        source="WS pre-check",
                        detail=px_err,
                    )
                    continue
            ready.append(aid)
        added = ready
        if not added:
            return
        bulk = len(added) > ws_bulk_refresh_threshold(cfg)
        if bulk:
            print(
                f"[WS-POOL] Mở {len(added)} WS song song — từng nick báo "
                f"🔐 token OK / ✅ WS đã kết nối",
                flush=True,
            )
        for aid in added:
            self._spawn_task(aid, lead=lead or aid, refresh=True)

    async def _stop_account(self, aid: str) -> None:
        if aid == self.listener_id:
            return
        from xoso66_ws_pool import clear_pending_ws_slot

        clear_pending_ws_slot(aid)
        task = self.tasks.pop(aid, None)
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def apply_pool(
        self,
        ids: list[str],
        *,
        cfg: dict[str, Any],
        refresh_new: bool = False,
    ) -> bool:
        """Áp dụng danh sách nick WS; trả True nếu có thay đổi."""
        from xoso66_shutdown import stopping

        if stopping():
            return False
        from xoso66_config_util import main_progress
        from xoso66_ws_pool import filter_ws_target_connectable

        raw_target = [
            str(x).strip()
            for x in ids
            if str(x).strip() and str(x).strip() != self.listener_id
        ]
        target = filter_ws_target_connectable(
            cfg, raw_target, ws_task_ids=list(self.tasks.keys())
        )
        current = set(self.tasks.keys())
        new_set = set(target)
        if not target and current:
            # Chỉ giữ WS khi đúng là còn lệnh cược pending của các nick hiện tại.
            # Nếu không còn pending mà target rỗng, phải cho phép ngắt WS để sync status.
            pending_ids: set[str] = set()
            try:
                from xoso66_auto_bet import pending_bet_account_ids

                pending_ids = {
                    str(x).strip() for x in pending_bet_account_ids() if str(x).strip()
                }
            except Exception:
                pending_ids = set()
            if pending_ids:
                keep_pending = sorted(a for a in current if a in pending_ids)
                if keep_pending:
                    target = keep_pending
                    new_set = set(target)
                    print(
                        f"[WS-POOL] Giữ {len(keep_pending)} nick WS — còn cược pending",
                        flush=True,
                    )
        if current == new_set and len(target) == len(self.tasks):
            self._sync_token_maintain_ids()
            return False

        removed = sorted(a for a in (current - new_set) if a != self.listener_id)
        added = sorted(new_set - set(self.tasks.keys()))

        for aid in removed:
            await self._stop_account(aid)
            if not stopping():
                await asyncio.to_thread(
                    _sync_ws_status_blocking,
                    cfg,
                    leaving=[aid],
                    joining=[],
                )

        self._sync_token_maintain_ids()

        lead = target[0] if target else ""
        if added:
            from xoso66_shutdown import stopping
            from xoso66_ws_pool import mark_pending_ws_slots

            if not stopping():
                mark_pending_ws_slots(added)
                for aid in added:
                    self._spawn_task(aid, lead=lead or aid, refresh=True)
                self._sync_token_maintain_ids()
                t = asyncio.create_task(
                    self._sync_joining_accounts(added, cfg=cfg),
                    name="ws-pool-sync-joining",
                )
                t.add_done_callback(_log_async_task_result)
        return True

    async def _sync_joining_accounts(
        self, added: list[str], *, cfg: dict[str, Any]
    ) -> None:
        from xoso66_shutdown import stopping

        if not added or stopping():
            return
        self._connect_batch_n += 1
        try:
            await asyncio.to_thread(
                _sync_ws_status_blocking,
                cfg,
                leaving=[],
                joining=added,
            )
        finally:
            self._connect_batch_n = max(0, self._connect_batch_n - 1)

    async def _connect_added_accounts(
        self,
        added: list[str],
        *,
        lead: str,
        refresh_new: bool,
        cfg: dict[str, Any],
    ) -> None:
        """Tương thích: spawn WS ngay, sync DB status nền."""
        if not added:
            return
        for aid in added:
            self._spawn_task(aid, lead=lead or aid, refresh=True)
        await self._sync_joining_accounts(added, cfg=cfg)

    async def _apply_pending_evictions(
        self, cfg: dict[str, Any]
    ) -> tuple[list[str], list[str]]:
        with _ws_evict_lock:
            evict = {x for x in _ws_evict_ids if x}
            _ws_evict_ids.clear()
        if self.listener_id:
            evict.discard(self.listener_id)
        if not evict:
            return list(self.tasks.keys()), []
        for aid in evict:
            await self._stop_account(aid)
        evicted = sorted(evict)
        await asyncio.to_thread(
            _sync_ws_status_blocking,
            cfg,
            leaving=evicted,
            joining=[],
        )
        return [a for a in self.tasks.keys() if a not in evict], evicted

    async def _connect_after_deposit(self, cfg: dict[str, Any]) -> bool:
        with _ws_after_deposit_lock:
            pending = sorted(_ws_after_deposit_ids)
            _ws_after_deposit_ids.clear()
        if not pending:
            return False
        min_bal = 0
        try:
            from xoso66_ws_pool import min_balance_for_ws

            min_bal = min_balance_for_ws(cfg)
        except Exception:
            pass
        from xoso66_accounts_db import get_account, username_for_log, usernames_for_log

        ready: list[str] = []
        for aid in pending:
            row = get_account(aid) or {}
            bal = float(row.get("balance") or 0)
            if bal >= min_bal:
                ready.append(aid)
            else:
                print(
                    f"[WS-POOL] Bỏ mở WS {username_for_log(aid)} — balance "
                    f"{bal:,.0f} < {min_bal:,}",
                    flush=True,
                )
        if not ready:
            return False
        from xoso66_ws_pool import ws_account_count, ws_slots_need_fill

        task_keys = list(self.tasks.keys())
        need = ws_slots_need_fill(cfg, task_ids=task_keys)
        cap = ws_account_count(cfg)
        if need <= 0:
            names = ", ".join(usernames_for_log(ready))
            print(
                f"[WS-POOL] Nạp Hoàn tất — pool WS đầy ({len(task_keys)}/{cap}), "
                f"chờ bù slot ở phiên mới: {names}",
                flush=True,
            )
            return False
        to_add = ready[:need]
        if len(to_add) < len(ready):
            skipped = ready[need:]
            print(
                f"[WS-POOL] Nạp Hoàn tất — chỉ bù {need} slot: "
                f"{usernames_for_log(to_add)}; chờ slot: "
                f"{usernames_for_log(skipped)}",
                flush=True,
            )
        target = sorted(set(task_keys) | set(to_add))
        return await self.apply_pool(target, cfg=cfg, refresh_new=True)

    async def resync_from_config(
        self, *, refresh_new: bool = False, round_start: bool = False
    ) -> bool:
        async with self._resync_lock:
            return await self._resync_from_config_impl(
                refresh_new=refresh_new,
                round_start=round_start,
            )

    async def _resync_from_config_impl(
        self, *, refresh_new: bool = False, round_start: bool = False
    ) -> bool:
        from xoso66_shutdown import stopping

        if stopping():
            return False
        from xoso66_accounts_db import username_for_log
        from xoso66_config_util import load_config, main_progress
        from xoso66_ws_pool import (
            _maybe_auto_switch_assign_strategy_when_no_ws_tasks,
            build_ws_sync_plan,
            get_connected_ws_accounts,
            get_pending_ws_slot_ids,
            schedule_fund_deposit_for_ws_shortage,
            ws_target_occupied_counts,
        )

        cfg = load_config()
        current, just_evicted = await self._apply_pending_evictions(cfg)
        changed = await self._connect_after_deposit(cfg)
        from xoso66_ws_pool import account_ws_deposit_busy, clear_pending_ws_slot

        task_ids = list(self.tasks.keys())
        connected = {
            str(x).strip()
            for x in get_connected_ws_accounts()
            if str(x).strip()
        }
        for aid in list(get_pending_ws_slot_ids()):
            if aid in self.tasks or aid in connected:
                continue
            if account_ws_deposit_busy(aid, cfg):
                continue
            clear_pending_ws_slot(aid)
        current = sorted(set(task_ids) | get_pending_ws_slot_ids())

        plan = build_ws_sync_plan(
            cfg,
            current,
            round_start=round_start,
            ws_task_ids=task_ids,
            just_evicted=just_evicted,
        )
        if plan is None:
            self._sync_token_maintain_ids()
            return changed

        if plan.prune_removed:
            for aid in plan.prune_removed:
                await self._stop_account(aid)
            if not stopping():
                await asyncio.to_thread(
                    _sync_ws_status_blocking,
                    cfg,
                    leaving=plan.prune_removed,
                    joining=[],
                )

        task_keys = set(self.tasks.keys())
        target_set = set(plan.target)
        connected_live = {
            str(x).strip()
            for x in get_connected_ws_accounts()
            if str(x).strip()
        }
        want_open = set(plan.fill_connect_ids or plan.connect_all or [])
        if round_start:
            to_open = sorted(a for a in want_open if a not in connected_live)
        else:
            to_open = sorted(want_open or (target_set - task_keys))
        from xoso66_cf import is_account_cf_rate_limited

        to_open = [a for a in to_open if not is_account_cf_rate_limited(a)]
        if to_open:
            tag = "Phiên mới" if round_start else "Resync"
            print(
                f"[WS-POOL] {tag} — bù WS: "
                f"{', '.join(username_for_log(a) for a in to_open[:12])}"
                f"{f'… +{len(to_open)-12}' if len(to_open) > 12 else ''}",
                flush=True,
            )
            for aid in to_open:
                if aid in self.tasks:
                    await self._stop_account(aid)
        pool_changed = bool(to_open) or target_set != task_keys
        if pool_changed and not stopping():
            changed = await self.apply_pool(
                plan.target, cfg=cfg, refresh_new=refresh_new
            ) or changed

        if plan.deposit_ids and not stopping():
            schedule_fund_deposit_for_ws_shortage(
                cfg, plan.deposit_ids, label="ws-pool-round-deposit"
            )

        occ = ws_target_occupied_counts(cfg)
        task_n = int(occ.get("task_n", 0))
        _maybe_auto_switch_assign_strategy_when_no_ws_tasks(
            cfg, task_n=task_n
        )

        self._sync_token_maintain_ids()
        return changed

    async def restart_dead_tasks(self) -> None:
        from xoso66_shutdown import stopping

        if not self.tasks or stopping():
            return
        with _token_maintain_lock:
            lead = _token_maintain_ids[0] if _token_maintain_ids else ""
        now = time.time()
        for aid, task in list(self.tasks.items()):
            if not task.done() or task.cancelled():
                continue
            from xoso66_cf import is_account_cf_rate_limited

            if is_account_cf_rate_limited(aid):
                continue
            last = self._last_respawn_at.get(aid, 0.0)
            if now - last < 25.0:
                continue
            exc = task.exception()
            if exc:
                from xoso66_accounts_db import username_for_log

                print(f"[WS-WORKER] {username_for_log(aid)} rớt WS: {exc}", flush=True)
            self._last_respawn_at[aid] = now
            self._spawn_task(aid, lead=lead or aid, refresh=True)

    async def shutdown(self) -> None:
        for aid in list(self.tasks.keys()):
            await self._stop_account(aid)
        await self._stop_listener()
        if self._token_thread and self._token_thread.is_alive():
            self._token_thread.join(timeout=3)


def schedule_ws_connect_after_deposit(account_ids: list[str]) -> None:
    """Sau nạp Hoàn tất — thêm nick vào pool WS (xử lý trên loop asyncio)."""
    from xoso66_shutdown import stopping

    if stopping():
        return
    from xoso66_ws_pool import (
        get_connected_ws_accounts,
        get_pending_ws_slot_ids,
        get_ws_task_accounts,
    )

    ids = [str(x).strip() for x in account_ids if str(x).strip()]
    if not ids:
        return
    occupied = (
        set(get_ws_task_accounts())
        | get_pending_ws_slot_ids()
        | {str(x).strip() for x in get_connected_ws_accounts() if str(x).strip()}
    )
    skipped = [x for x in ids if x in occupied]
    ids = [x for x in ids if x not in occupied]
    if skipped:
        from xoso66_accounts_db import usernames_for_log

        print(
            f"[WS-POOL] Bỏ lên lịch mở WS (đã task/pending/connect): "
            f"{usernames_for_log(skipped)}",
            flush=True,
        )
    if not ids:
        return
    with _ws_after_deposit_lock:
        _ws_after_deposit_ids.update(ids)
    _ws_after_deposit_check.set()


def schedule_ws_pool_round_check() -> None:
    """Gọi từ handler BẮT ĐẦU PHIÊN (thread sync) → resync pool trên loop WS."""
    from xoso66_shutdown import stopping

    if stopping():
        return
    _ws_pool_round_check.set()


def cancel_ws_pool_pending_work() -> None:
    """Ctrl+C — bỏ resync/nạp WS đã lên lịch."""
    _ws_pool_round_check.clear()
    _ws_after_deposit_check.clear()
    with _ws_after_deposit_lock:
        _ws_after_deposit_ids.clear()


def schedule_ws_evict_and_resync(account_ids: list[str]) -> None:
    """Ngắt WS nick đã gần đủ cap cược ngày, bổ sung nick mới."""
    from xoso66_shutdown import stopping

    if stopping():
        return
    from xoso66_config_util import load_config
    from xoso66_ws_pool import filter_ws_evict_ids

    cfg = load_config()
    aids = filter_ws_evict_ids(
        [str(x).strip() for x in account_ids if str(x).strip()], cfg
    )
    if not aids:
        return
    with _ws_evict_lock:
        _ws_evict_ids.update(aids)
    _ws_pool_round_check.set()


async def run_managed_ws_workers(
    initial_ids: list[str] | None = None,
    *,
    refresh_before_connect: bool = True,
) -> None:
    from xoso66_config_util import load_config
    from xoso66_shutdown import stopping
    from xoso66_ws_pool import ws_pool_resync_enabled, ws_pool_resync_interval_sec

    from xoso66_ws_pool import register_ws_pool_round_handler

    register_ws_pool_round_handler()

    global _active_ws_supervisor, _active_ws_loop

    sup = WsPoolSupervisor()
    from xoso66_ws_pool import register_ws_task_ids_provider

    register_ws_task_ids_provider(lambda: list(sup.tasks.keys()))
    sup._ensure_token_thread()
    cfg = load_config()

    await sup.ensure_listener(cfg)
    if initial_ids:
        await sup.apply_pool(initial_ids, cfg=cfg, refresh_new=refresh_before_connect)
    else:
        await sup.resync_from_config(refresh_new=refresh_before_connect)
    await sup.ensure_listener(cfg)

    from xoso66_ws_pool import enable_ws_round_sync

    enable_ws_round_sync()
    print(
        "[WS-POOL] WS chạy nền — auto-bet/cược khi có nick connect (không chờ hết 56)",
        flush=True,
    )

    async def _ws_connect_progress() -> None:
        from xoso66_ws_pool import get_connected_ws_accounts

        last = -1
        while not stopping():
            await asyncio.sleep(12)
            if stopping():
                break
            n_conn = len(get_connected_ws_accounts())
            n_task = len(sup.tasks)
            if n_task and n_conn != last:
                print(
                    f"[WS-POOL] Đã connect WS: {n_conn}/{n_task}",
                    flush=True,
                )
                last = n_conn
            if n_task and n_conn >= n_task:
                break

    progress_task: asyncio.Task[None] | None = asyncio.create_task(
        _ws_connect_progress()
    )

    resync_on = ws_pool_resync_enabled(cfg)
    interval = ws_pool_resync_interval_sec(cfg)

    def _maybe_bootstrap_playing_game() -> None:
        try:
            from xoso66_auto_bet import try_bootstrap_playing_game

            try_bootstrap_playing_game(load_config())
        except Exception as e:
            print(f"[AUTO-BET] Bootstrap chọn game: {e}", flush=True)

    threading.Thread(
        target=_maybe_bootstrap_playing_game,
        name="xoso66-bootstrap-playing",
        daemon=True,
    ).start()

    try:
        while not stopping():
            await sup.ensure_listener(cfg)
            if (
                not sup.tasks
                and not sup._pool_slots_busy()
                and not stopping()
            ):
                await sup.resync_from_config(refresh_new=False)

            wait_sec = interval if resync_on else 30
            listener_tasks: list[asyncio.Task[None]] = []
            if sup.listener_task is not None:
                listener_tasks = [sup.listener_task]
            ws_tasks = list(sup.tasks.values())
            sleep_task = asyncio.create_task(asyncio.sleep(wait_sec))
            pending = set(ws_tasks + listener_tasks + [sleep_task])
            done: set[asyncio.Task] = set()
            while pending and not stopping():
                finished, pending = await asyncio.wait(
                    pending,
                    timeout=1.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                done |= finished
                if _ws_pool_round_check.is_set() or _ws_after_deposit_check.is_set():
                    break
            if stopping():
                for t in pending:
                    t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                _ws_pool_round_check.clear()
                _ws_after_deposit_check.clear()
                break
            if _ws_after_deposit_check.is_set() and not stopping():
                _ws_after_deposit_check.clear()
                try:
                    await sup.resync_from_config(
                        refresh_new=True, round_start=False
                    )
                except Exception as e:
                    print(f"[WS-POOL] Resync sau nạp lỗi: {e}", flush=True)
            if _ws_pool_round_check.is_set() and not stopping():
                _ws_pool_round_check.clear()
                try:
                    await sup.resync_from_config(
                        refresh_new=False, round_start=True
                    )
                except Exception as e:
                    print(f"[WS-POOL] Resync đầu phiên lỗi: {e}", flush=True)
            elif sleep_task in done and resync_on and not stopping():
                try:
                    await sup.resync_from_config(refresh_new=False)
                except Exception as e:
                    print(f"[WS-POOL] Resync định kỳ lỗi: {e}", flush=True)
            if sleep_task in pending or sleep_task in done:
                sleep_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sleep_task
            if not stopping():
                await sup.restart_dead_tasks()
                await sup.restart_listener_if_dead(cfg)
    finally:
        if progress_task is not None:
            progress_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await progress_task
        await sup.shutdown()
        pending_tasks = [
            t
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]
        for t in pending_tasks:
            t.cancel()
        if pending_tasks:
            with contextlib.suppress(Exception):
                await asyncio.gather(*pending_tasks, return_exceptions=True)
        if stopping():
            print("[WS-WORKER] Đã dừng (Ctrl+C).", flush=True)


async def run_dual_ws_workers(
    account_ids: list[str] | None = None,
    *,
    ws_count: int = WS_WORKER_COUNT,
    refresh_before_connect: bool = True,
) -> None:
    """Tương thích cũ — không resync định kỳ."""
    from xoso66_shutdown import stopping

    if account_ids:
        if len(account_ids) < ws_count:
            raise RuntimeError(f"Cần {ws_count} account_id, nhận {len(account_ids)}")
        picked = account_ids[:ws_count]
    else:
        from xoso66_config_util import load_config

        picked = select_ws_account_ids(load_config())[:ws_count]

    from xoso66_config_util import load_config

    sup = WsPoolSupervisor()
    sup._ensure_token_thread()
    cfg = load_config()
    await sup.ensure_listener(cfg)
    await sup.apply_pool(picked, cfg=cfg, refresh_new=refresh_before_connect)
    try:
        gather_tasks = list(sup.tasks.values())
        if sup.listener_task is not None:
            gather_tasks.append(sup.listener_task)
        await asyncio.gather(*gather_tasks)
    except asyncio.CancelledError:
        pass
    finally:
        await sup.shutdown()
        if stopping():
            print("[WS-WORKER] Đã dừng (Ctrl+C).", flush=True)


def run_ws_worker_blocking(
    account_ids: list[str] | None = None,
    *,
    ws_count: int = WS_WORKER_COUNT,
    refresh_before_connect: bool = True,
) -> None:
    from xoso66_config_util import load_config
    from xoso66_ws_pool import ws_pool_resync_enabled

    cfg = load_config()
    ids = account_ids
    # Không cắt theo ws_account_count — giá trị đó chỉ là giới hạn slot WS tối đa.
    if ws_pool_resync_enabled(cfg):
        asyncio.run(
            run_managed_ws_workers(
                ids,
                refresh_before_connect=refresh_before_connect,
            )
        )
    else:
        asyncio.run(
            run_dual_ws_workers(
                ids,
                ws_count=ws_count or ws_account_count(cfg),
                refresh_before_connect=refresh_before_connect,
            )
        )


def start_ws_worker_thread(
    account_ids: list[str] | None = None,
    *,
    ws_count: int = WS_WORKER_COUNT,
) -> threading.Thread:
    """Chạy worker WS nền (daemon) — dùng từ main.py."""

    def _target() -> None:
        try:
            run_ws_worker_blocking(account_ids, ws_count=ws_count)
        except Exception as e:
            print(f"[WS-WORKER] Dừng: {e}", flush=True)

    t = threading.Thread(target=_target, name="xoso66-minigame-ws", daemon=False)
    t.start()
    return t


def main() -> int:
    ap = argparse.ArgumentParser(description="WS worker: 2 acc Đang Chơi, 5 game hũ")
    ap.add_argument(
        "-a",
        "--account",
        action="append",
        default=[],
        help="acc id cố định (lặp 2 lần hoặc kèm -a acc2); mặc định random Đang Chơi",
    )
    ap.add_argument("--count", type=int, default=WS_WORKER_COUNT)
    ap.add_argument("--no-refresh", action="store_true", help="không refresh token trước connect")
    ap.add_argument(
        "--watch-games",
        default="",
        help="ghi đè game_id theo dõi, VD: all hoặc 9,17,18,19,2",
    )
    args = ap.parse_args()

    global WATCH_GAME_IDS, _cli_watch_override
    if (args.watch_games or "").strip():
        WATCH_GAME_IDS = parse_watch_game_ids(args.watch_games)
        _cli_watch_override = True

    init_db()
    from xoso66_config_util import load_config

    cfg = load_config()
    ids = [str(x).strip() for x in args.account if str(x).strip()]
    if not ids:
        from xoso66_ws_pool import prepare_ws_pool

        ids = prepare_ws_pool(cfg)
    try:
        run_ws_worker_blocking(
            ids or None,
            ws_count=max(1, len(ids) if ids else args.count),
            refresh_before_connect=not args.no_refresh,
        )
    except KeyboardInterrupt:
        print("\n[WS-WORKER] Dừng.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
