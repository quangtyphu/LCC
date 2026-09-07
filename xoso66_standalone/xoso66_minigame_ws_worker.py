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
import uuid
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

_ws_pool_round_check = threading.Event()  # Chỉ BẮT ĐẦU phiên (game theo dõi)
_ws_pool_resync_check = threading.Event()  # Evict / stale / bù slot — Resync thường
_ws_after_deposit_check = threading.Event()
_ws_evict_ids: set[str] = set()
_ws_evict_lock = threading.Lock()
_ws_after_deposit_ids: set[str] = set()
_ws_after_deposit_lock = threading.Lock()
# Dedup: một issue chỉ chạy 5 việc «Phiên mới» một lần.
_ws_round_resync_done: tuple[int, str] | None = None
_ws_round_resync_pending: tuple[int, str] | None = None
_ws_round_resync_lock = threading.Lock()
# Snapshot pool gần nhất — soft restart sau crash socket không cold-start 0 nick.
_last_ws_pool_snapshot: list[str] = []
_last_ws_pool_snapshot_lock = threading.Lock()

WATCH_GAME_IDS = frozenset(DEFAULT_JACKPOT_GAME_IDS)
_cli_watch_override = False

_active_ws_supervisor: Any = None
_active_ws_loop: asyncio.AbstractEventLoop | None = None


def remember_ws_pool_snapshot(account_ids: list[str] | set[str]) -> None:
    global _last_ws_pool_snapshot
    cleaned = [str(x).strip() for x in account_ids if str(x).strip()]
    if not cleaned:
        return
    with _last_ws_pool_snapshot_lock:
        _last_ws_pool_snapshot = list(dict.fromkeys(cleaned))


def get_last_ws_pool_snapshot() -> list[str]:
    with _last_ws_pool_snapshot_lock:
        return list(_last_ws_pool_snapshot)


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
        if _is_ws_shutdown_error(e):
            return
        if _is_transient_ws_loop_error(e):
            # Lỗi 1 nick — không spam / không lan.
            return
        print(f"[WS-POOL] Task {task.get_name() or '?'} lỗi: {e}", flush=True)


def _is_ws_shutdown_error(exc: BaseException) -> bool:
    """Chỉ CancelledError = shutdown task. Không coi GeneratorExit là thoát pool."""
    return isinstance(exc, asyncio.CancelledError)


def _is_event_loop_dead_error(exc: BaseException | None) -> bool:
    """Loop đã đóng / không còn running — phải soft-restart, không spin giữ worker."""
    if not isinstance(exc, RuntimeError):
        return False
    s = str(exc).lower()
    return (
        "no running event loop" in s
        or "event loop is closed" in s
        or "attached to a different loop" in s
    )


def _is_transient_ws_loop_error(exc: BaseException | None) -> bool:
    """Lỗi socket Windows (10038) khi proxy/task rớt — không thoát / không restart toàn worker."""
    if _is_event_loop_dead_error(exc):
        return False
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, OSError):
            winerr = getattr(cur, "winerror", None)
            if winerr in (10038, 10054, 10053, 995, 64, 1236):
                return True
            errno = getattr(cur, "errno", None)
            if errno in (9, 10038, 10054, 10053):  # EBADF / WSA*
                return True
            msg = str(cur).lower()
            if (
                "not a socket" in msg
                or "10038" in msg
                or "forcibly closed" in msg
                or "connection reset" in msg
            ):
                return True
        if isinstance(cur, ConnectionError):
            return True
        if isinstance(cur, RuntimeError):
            s = str(cur).lower()
            if "generatorexit" in s or "destroyed but it is pending" in s:
                return True
        cur = cur.__cause__ or cur.__context__
    return False


def _install_loop_exception_handler(loop: asyncio.AbstractEventLoop) -> None:
    def _handler(
        _loop: asyncio.AbstractEventLoop, context: dict[str, Any]
    ) -> None:
        exc = context.get("exception")
        msg = str(context.get("message") or "")
        if exc is not None:
            if isinstance(exc, asyncio.CancelledError):
                return
            if _is_ws_shutdown_error(exc):
                return
            if _is_transient_ws_loop_error(exc):
                print(f"[WS-POOL] Loop socket (bỏ qua): {exc}", flush=True)
                return
            print(f"[WS-POOL] Loop exception: {exc} | {msg}", flush=True)
        elif msg:
            if "destroyed but it is pending" in msg.lower():
                return
            print(f"[WS-POOL] Loop: {msg}", flush=True)

    loop.set_exception_handler(_handler)


async def _safe_asyncio_wait(
    pending: set[asyncio.Task[Any]],
    *,
    timeout: float,
    return_when: Any,
) -> tuple[set[asyncio.Task[Any]], set[asyncio.Task[Any]]]:
    """asyncio.wait — bắt WinError 10038 (Windows) thay vì sập cả worker."""
    try:
        return await asyncio.wait(
            pending,
            timeout=timeout,
            return_when=return_when,
        )
    except Exception as e:
        if not _is_transient_ws_loop_error(e):
            raise
        print(f"[WS-POOL] asyncio.wait socket: {e} — giữ loop", flush=True)
        return set(), {t for t in pending if not t.done()}


def _game_worker_float(cfg: dict[str, Any], key: str, default: float) -> float:
    gw = cfg.get("game_worker") if isinstance(cfg.get("game_worker"), dict) else {}
    try:
        return max(0.0, float(gw.get(key, default)))
    except (TypeError, ValueError):
        return default


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


def note_ws_task_activity(account_id: str) -> None:
    """
    Giữ API tương thích.
    Không gia hạn _task_started_at khi chưa connect — prune 60s phải bắt
    task kẹt reconnect mãi (tránh connect=2/N zombie).
    """
    _ = account_id
    return


async def run_ws_for_account(
    account_id: str,
    *,
    jackpot_store: MinigameJackpotStore,
    subscribe_spec: str,
    broadcast: WsBroadcastCoordinator,
    refresh_before_connect: bool = True,
    conn_gen: str | None = None,
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
                conn_gen=conn_gen,
            )
        except asyncio.CancelledError:
            raise
        except RuntimeError as e:
            if stopping():
                return
            if _is_transient_ws_loop_error(e):
                print(
                    f"[WS] [{user}] runtime tạm ({e}) — chỉ nick này, "
                    f"giữ pool; thử lại sau 7s",
                    flush=True,
                )
                from xoso66_ws_pool import unregister_ws_connected

                unregister_ws_connected(aid, conn_gen=conn_gen)
                for _ in range(7):
                    if stopping():
                        return
                    await asyncio.sleep(1)
                continue
            raise
        except Exception as e:
            from xoso66_cf import CfRateLimitError

            if stopping():
                return

            if isinstance(e, CfRateLimitError):
                print(f"❌ [{user}] WS: {e}", flush=True)
                await _sleep_until_account_cf_cooldown(aid, user)
                continue
            # WinError 10038 / proxy chết: chỉ nick này nghỉ rồi mở lại — không raise.
            if _is_transient_ws_loop_error(e):
                print(
                    f"[WS] [{user}] socket tạm ({e}) — chỉ nick này, "
                    f"giữ pool; thử lại sau 7s",
                    flush=True,
                )
                from xoso66_ws_pool import unregister_ws_connected

                unregister_ws_connected(aid, conn_gen=conn_gen)
                for _ in range(7):
                    if stopping():
                        return
                    await asyncio.sleep(1)
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
        self._task_started_at: dict[str, float] = {}
        self._conn_gen: dict[str, str] = {}

    def _sync_token_maintain_ids(self) -> None:
        ids = list(self.tasks.keys())
        if self.listener_id and self.listener_id not in ids:
            ids.append(self.listener_id)
        remember_ws_pool_snapshot(ids)
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
        gen = uuid.uuid4().hex
        self._conn_gen[aid] = gen
        self.listener_task = asyncio.create_task(
            run_ws_for_account(
                aid,
                jackpot_store=self.jp_store,
                subscribe_spec=DEFAULT_WS_SUBSCRIBE,
                broadcast=self.broadcast,
                refresh_before_connect=refresh,
                conn_gen=gen,
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

    def _spawn_task(self, aid: str, *, lead: str, refresh: bool) -> bool:
        """Một nick chỉ một task WS; không ghi đè task đang chạy. Trả True nếu spawn."""
        aid = str(aid).strip()
        if not aid or aid == self.listener_id:
            return False
        _ = lead
        # Không check balance / proxy / deposit — chỉ spawn mở WS; lỗi tính sau.
        with self._spawn_lock:
            old = self.tasks.get(aid)
            if old is not None and not old.done():
                return False
            if old is not None:
                old.cancel()
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return False
            gen = uuid.uuid4().hex
            self._conn_gen[aid] = gen
            task = loop.create_task(
                run_ws_for_account(
                    aid,
                    jackpot_store=self.jp_store,
                    subscribe_spec=DEFAULT_WS_SUBSCRIBE,
                    broadcast=self.broadcast,
                    refresh_before_connect=False,
                    conn_gen=gen,
                ),
                name=f"ws-{aid}",
            )
            task.add_done_callback(_log_async_task_result)
            self.tasks[aid] = task
            self._task_started_at[aid] = time.time()
            return True

    async def _spawn_staggered(
        self,
        aids: list[str],
        *,
        lead: str,
        refresh: bool,
        cfg: dict[str, Any] | None = None,
    ) -> list[str]:
        """Spawn tuần tự + delay (Windows 0.5s) — tránh 10038 khi mở hàng loạt SOCKS."""
        from xoso66_config_util import load_config
        from xoso66_shutdown import stopping
        from xoso66_ws_pool import ws_connect_batch_delay_sec

        if cfg is None:
            cfg = load_config()
        delay = ws_connect_batch_delay_sec(cfg)
        spawned: list[str] = []
        for i, aid in enumerate(aids):
            if stopping():
                break
            if i > 0 and delay > 0:
                await asyncio.sleep(delay)
            if self._spawn_task(aid, lead=lead or aid, refresh=refresh):
                spawned.append(aid)
        return spawned

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
        _ = refresh_new
        if not added:
            return
        # Chỉ bỏ listener — không check balance / proxy / nạp.
        ready = [
            str(a).strip()
            for a in added
            if str(a).strip() and str(a).strip() != self.listener_id
        ]
        if not ready:
            return
        print(
            f"[WS-POOL] Mở {len(ready)} WS tuần tự — chỉ connect (proxy)",
            flush=True,
        )
        await self._spawn_staggered(
            ready, lead=lead or ready[0], refresh=False, cfg=cfg
        )

    async def _stop_account(self, aid: str) -> None:
        if aid == self.listener_id:
            return
        from xoso66_ws_pool import clear_pending_ws_slot, unregister_ws_connected

        clear_pending_ws_slot(aid)
        self._task_started_at.pop(aid, None)
        self._conn_gen.pop(aid, None)
        # Luôn gỡ khỏi set connect — tránh ghost chiếm slot khi task đã done
        # mà status đã Đủ ngày/Hết Tiền.
        unregister_ws_connected(aid)
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

        raw_target = [
            str(x).strip()
            for x in ids
            if str(x).strip() and str(x).strip() != self.listener_id
        ]
        # Tin list đầu vào — không filter balance / pending / proxy lúc mở.
        target = list(dict.fromkeys(raw_target))
        current = set(self.tasks.keys())
        new_set = set(target)
        # Luôn giữ nick còn lệnh cược chờ KQ — kể cả khi target còn nick khác.
        pending_ids: set[str] = set()
        try:
            from xoso66_auto_bet import pending_bet_account_ids

            pending_ids = {
                str(x).strip() for x in pending_bet_account_ids() if str(x).strip()
            }
        except Exception:
            pending_ids = set()
        keep_pending = sorted(a for a in current if a in pending_ids)
        if keep_pending:
            before = set(target)
            target = list(dict.fromkeys([*target, *keep_pending]))
            new_set = set(target)
            added_keep = [a for a in keep_pending if a not in before]
            if added_keep:
                from xoso66_accounts_db import username_for_log

                names = ", ".join(username_for_log(a) for a in added_keep[:8])
                extra = f"… +{len(added_keep) - 8}" if len(added_keep) > 8 else ""
                print(
                    f"[WS-POOL] Giữ WS — còn lệnh chờ KQ: {names}{extra}",
                    flush=True,
                )
        if current == new_set and len(target) == len(self.tasks):
            self._sync_token_maintain_ids()
            return False

        removed = sorted(
            a
            for a in (current - new_set)
            if a != self.listener_id and a not in pending_ids
        )
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
            from xoso66_ws_pool import mark_pending_ws_slots, ws_account_count

            # Không spawn vượt ws_account_count (sau khi đã stop removed).
            cap = ws_account_count(cfg)
            room = max(0, cap - len(self.tasks))
            if len(added) > room:
                skipped = added[room:]
                added = added[:room]
                if skipped:
                    from xoso66_accounts_db import usernames_for_log

                    print(
                        f"[WS-POOL] Cắt spawn vượt cap {cap}: giữ {len(added)}, "
                        f"bỏ {usernames_for_log(skipped[:8])}"
                        f"{f'… +{len(skipped)-8}' if len(skipped) > 8 else ''}",
                        flush=True,
                    )

            spawned: list[str] = []
            if not stopping() and added:
                # Tôn trọng refresh_new — trước đây luôn True → mỗi lần bù ép lấy lại
                # ws_token + chậm hàng chục giây / nick.
                spawned = await self._spawn_staggered(
                    added,
                    lead=lead or added[0],
                    refresh=bool(refresh_new),
                    cfg=cfg,
                )
                if spawned:
                    mark_pending_ws_slots(spawned)
                    self._sync_token_maintain_ids()
                    n = len(spawned)
                    mode = "chỉ connect"
                    print(
                        f"[WS-POOL] Đang mở {n} WS tuần tự ({mode})…",
                        flush=True,
                    )
                    t = asyncio.create_task(
                        self._sync_joining_accounts(spawned, cfg=cfg),
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
        """Tương thích: spawn WS tuần tự, sync DB status nền."""
        if not added:
            return
        await self._spawn_staggered(
            added, lead=lead or added[0], refresh=bool(refresh_new), cfg=cfg
        )
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
        from xoso66_ws_pool import (
            count_ws_slots_in_use,
            ws_account_count,
            ws_slots_need_fill,
        )

        need = ws_slots_need_fill(cfg, task_ids=list(self.tasks.keys()))
        cap = ws_account_count(cfg)
        info = count_ws_slots_in_use(cfg, task_ids=list(self.tasks.keys()))
        if need <= 0:
            names = ", ".join(usernames_for_log(ready))
            print(
                f"[WS-POOL] Nạp Hoàn tất — đủ «Đang Chơi» "
                f"({info['in_use_n']}/{cap}), chờ bù slot: {names}",
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
        # Chỉ giữ task đã connect / pending — không giữ zombie trong target.
        from xoso66_ws_pool import get_connected_ws_accounts, get_pending_ws_slot_ids

        keep_tasks = {
            a
            for a in self.tasks
            if a in get_connected_ws_accounts() or a in get_pending_ws_slot_ids()
        }
        target = sorted(keep_tasks | set(to_add))
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
        # Task đang chạy (kể cả connect dở): giữ nguyên — không stop/spawn lại mỗi phiên.
        # Chỉ bù nick chưa có task sống; task đã chết (done) thì pop rồi apply_pool mở lại.
        running = {
            a
            for a, t in self.tasks.items()
            if t is not None and not t.done()
        }
        need_spawn = [a for a in to_open if a not in running]
        # Không spawn vượt cap (task đang chạy + sẽ spawn).
        from xoso66_ws_pool import ws_account_count as _ws_cap

        cap = _ws_cap(cfg)
        room = max(0, cap - len(running))
        if len(need_spawn) > room:
            skipped = need_spawn[room:]
            need_spawn = need_spawn[:room]
            if skipped:
                print(
                    f"[WS-POOL] Cắt bù WS vượt cap {cap}: giữ {len(need_spawn)}, "
                    f"bỏ {', '.join(username_for_log(a) for a in skipped[:8])}"
                    f"{f'… +{len(skipped)-8}' if len(skipped) > 8 else ''}",
                    flush=True,
                )
        for aid in need_spawn:
            t = self.tasks.get(aid)
            if t is not None and t.done():
                await self._stop_account(aid)
        if need_spawn:
            tag = "Phiên mới" if round_start else "Resync"
            print(
                f"[WS-POOL] {tag} — bù WS: "
                f"{', '.join(username_for_log(a) for a in need_spawn[:12])}"
                f"{f'… +{len(need_spawn)-12}' if len(need_spawn) > 12 else ''}",
                flush=True,
            )
            # Spawn thẳng list đã chọn — KHÔNG qua filter / balance / proxy check.
            from xoso66_ws_pool import mark_pending_ws_slots

            lead = need_spawn[0]
            spawned = await self._spawn_staggered(
                need_spawn, lead=lead or need_spawn[0], refresh=False, cfg=cfg
            )
            if spawned:
                mark_pending_ws_slots(spawned)
                self._sync_token_maintain_ids()
                print(
                    f"[WS-POOL] Đang mở {len(spawned)}/{len(need_spawn)} WS "
                    f"tuần tự (chỉ connect)…",
                    flush=True,
                )
                t = asyncio.create_task(
                    self._sync_joining_accounts(spawned, cfg=cfg),
                    name="ws-pool-sync-joining",
                )
                t.add_done_callback(_log_async_task_result)
                changed = True
            else:
                print(
                    f"[WS-POOL] {tag} — spawn 0/{len(need_spawn)} "
                    f"(đã có task sống)",
                    flush=True,
                )
        # Đồng bộ target còn lại (đóng thừa) — không spawn lại qua filter.
        apply_ids = list(
            dict.fromkeys(
                [
                    *(a for a in plan.target if a in running),
                    *need_spawn,
                    *(a for a in running if a in target_set),
                ]
            )
        )
        # Chỉ stop nick không còn trong apply_ids (không spawn qua apply_pool).
        to_stop = [
            a
            for a in list(self.tasks.keys())
            if a not in apply_ids and a != self.listener_id
        ]
        if to_stop and not stopping():
            for aid in to_stop:
                await self._stop_account(aid)
            await asyncio.to_thread(
                _sync_ws_status_blocking,
                cfg,
                leaving=to_stop,
                joining=[],
            )
            changed = True

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
        from xoso66_config_util import load_config
        from xoso66_ws_pool import (
            clear_pending_ws_slot,
            is_ws_pool_active_status,
            unregister_ws_connected,
        )
        from xoso66_accounts_db import get_account, username_for_log

        cfg = load_config()
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
            row = get_account(aid) or {}
            bet_pending = False
            with contextlib.suppress(Exception):
                from xoso66_auto_bet import pending_bet_account_ids

                bet_pending = aid in pending_bet_account_ids()
            # Sai status → không respawn (tránh ghost Đủ ngày/Hết Tiền).
            if not is_ws_pool_active_status(row, cfg) and not bet_pending:
                self.tasks.pop(aid, None)
                clear_pending_ws_slot(aid)
                unregister_ws_connected(aid)
                continue
            # Không check balance — cứ mở lại WS; lỗi tính sau.
            if exc:
                print(f"[WS-WORKER] {username_for_log(aid)} rớt WS: {exc}", flush=True)
            self._last_respawn_at[aid] = now
            self._spawn_task(aid, lead=lead or aid, refresh=False)

    async def prune_stale_unconnected_tasks(self, cfg: dict[str, Any]) -> None:
        """
        Task sống nhưng chưa connect >60s / proxy chết.
        Proxy chết: gỡ slot. Còn lại: stop + spawn lại cùng nick (LC79-style).
        """
        from xoso66_shutdown import stopping

        if stopping() or not self.tasks:
            return
        from xoso66_accounts_db import username_for_log
        from xoso66_proxy import is_proxy_dead
        from xoso66_ws_pool import (
            clear_pending_ws_slot,
            get_connected_ws_accounts,
            mark_pending_ws_slots,
            schedule_fund_deposit_for_ws_shortage,
        )

        connected = {
            str(x).strip() for x in get_connected_ws_accounts() if str(x).strip()
        }
        bet_pending: set[str] = set()
        with contextlib.suppress(Exception):
            from xoso66_auto_bet import pending_bet_account_ids

            bet_pending = {
                str(x).strip() for x in pending_bet_account_ids() if str(x).strip()
            }
        now = time.time()
        stale_sec = 60.0
        stale: list[str] = []
        for aid, task in list(self.tasks.items()):
            if aid == self.listener_id:
                continue
            if aid in bet_pending:
                continue
            if aid in connected:
                continue
            if task is None or task.done():
                continue
            started = float(self._task_started_at.get(aid) or 0.0)
            age = (now - started) if started > 0 else stale_sec + 1.0
            proxy_dead = False
            with contextlib.suppress(Exception):
                proxy_dead = bool(is_proxy_dead(aid))
            if proxy_dead or age >= stale_sec:
                stale.append(aid)
        if not stale:
            return
        names = ", ".join(username_for_log(a) for a in stale[:10])
        extra = f"… +{len(stale) - 10}" if len(stale) > 10 else ""
        dead = [a for a in stale if is_proxy_dead(a)]
        alive = [a for a in stale if a not in dead]
        if dead:
            print(
                f"[WS-POOL] Gỡ task proxy chết: "
                f"{', '.join(username_for_log(a) for a in dead[:10])}"
                f"{f'… +{len(dead)-10}' if len(dead) > 10 else ''} "
                f"— nhường slot",
                flush=True,
            )
            for aid in dead:
                await self._stop_account(aid)
                clear_pending_ws_slot(aid)
        if alive:
            print(
                f"[WS-POOL] Respawn chưa connect >{stale_sec:.0f}s: {names}{extra}",
                flush=True,
            )
            with _token_maintain_lock:
                lead = (
                    _token_maintain_ids[0]
                    if _token_maintain_ids
                    else (alive[0] if alive else "")
                )
            for aid in alive:
                await self._stop_account(aid)
                clear_pending_ws_slot(aid)
            spawned = await self._spawn_staggered(
                alive, lead=lead or alive[0], refresh=False, cfg=cfg
            )
            if spawned:
                mark_pending_ws_slots(spawned)
                self._sync_token_maintain_ids()
            with contextlib.suppress(Exception):
                schedule_fund_deposit_for_ws_shortage(
                    cfg, alive, label="ws-stale-unconnected"
                )

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


def schedule_ws_pool_round_check(
    *, game_id: int | None = None, issue: str = ""
) -> None:
    """
    Chỉ từ handler BẮT ĐẦU PHIÊN (game đang theo dõi).
    Mỗi (game_id, issue) chỉ xếp / chạy 5 việc «Phiên mới» một lần.
    """
    from xoso66_shutdown import stopping

    if stopping():
        return
    global _ws_round_resync_pending
    key: tuple[int, str] | None = None
    if game_id is not None and str(issue or "").strip():
        key = (int(game_id), str(issue).strip())
    with _ws_round_resync_lock:
        if key is not None:
            if _ws_round_resync_done == key:
                return
            if _ws_round_resync_pending == key and _ws_pool_round_check.is_set():
                return
            _ws_round_resync_pending = key
        _ws_pool_round_check.set()


def schedule_ws_pool_resync_check() -> None:
    """Resync thường (evict / stale / bù) — không gắn nhãn Phiên mới."""
    from xoso66_shutdown import stopping

    if stopping():
        return
    _ws_pool_resync_check.set()


def cancel_ws_pool_pending_work() -> None:
    """Ctrl+C — bỏ resync/nạp WS đã lên lịch."""
    _ws_pool_round_check.clear()
    _ws_pool_resync_check.clear()
    _ws_after_deposit_check.clear()
    with _ws_after_deposit_lock:
        _ws_after_deposit_ids.clear()
    with _ws_round_resync_lock:
        global _ws_round_resync_pending
        _ws_round_resync_pending = None


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
    schedule_ws_pool_resync_check()


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
    _active_ws_supervisor = sup
    _active_ws_loop = asyncio.get_running_loop()
    from xoso66_ws_pool import register_ws_task_ids_provider

    register_ws_task_ids_provider(
        lambda: [
            a
            for a, t in sup.tasks.items()
            if t is not None and not t.done()
        ]
    )
    sup._ensure_token_thread()
    cfg = load_config()

    await sup.ensure_listener(cfg)
    try:
        if initial_ids:
            await sup.apply_pool(initial_ids, cfg=cfg, refresh_new=refresh_before_connect)
        else:
            await sup.resync_from_config(refresh_new=refresh_before_connect)
        await sup.ensure_listener(cfg)
    except Exception as e:
        if _is_transient_ws_loop_error(e):
            print(
                f"[WS-POOL] Lỗi socket lúc mở pool ban đầu ({e}) — "
                f"tiếp tục vòng quản lý, không sập worker",
                flush=True,
            )
        else:
            raise

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

    health_interval = _game_worker_float(cfg, "ws_health_log_interval_sec", 120.0)

    async def _ws_health_log() -> None:
        from xoso66_accounts_db import username_for_log
        from xoso66_ws_pool import get_connected_ws_accounts

        while not stopping():
            await asyncio.sleep(max(30.0, health_interval))
            if stopping():
                break
            n_conn = len(get_connected_ws_accounts())
            n_task = len(sup.tasks)
            listener = sup.listener_id
            listener_ok = (
                sup.listener_task is not None and not sup.listener_task.done()
            )
            listener_user = username_for_log(listener) if listener else "—"
            listener_state = "OK" if listener_ok else "RỚT"
            print(
                f"[WS-HEALTH] connect={n_conn}/{n_task} "
                f"listener={listener_user} ({listener_state})",
                flush=True,
            )

    health_task: asyncio.Task[None] | None = None
    if health_interval > 0:
        health_task = asyncio.create_task(_ws_health_log())

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

    async def _supervise_rounds() -> None:
        """Vòng wait/resync — lỗi socket giữ worker; loop chết → raise soft-restart."""
        consec_other = 0
        while not stopping():
            try:
                await _managed_ws_loop_once(
                    sup,
                    cfg,
                    resync_on=resync_on,
                    interval=interval,
                )
                consec_other = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if stopping():
                    return
                if _is_event_loop_dead_error(e):
                    print(
                        f"[WS-POOL] Event loop chết ({e}) — "
                        f"thoát để soft-restart (không spin)",
                        flush=True,
                    )
                    raise
                if _is_ws_shutdown_error(e):
                    return
                transient = _is_transient_ws_loop_error(e)
                if transient:
                    consec_other = 0
                    print(
                        f"[WS-POOL] Socket tạm ({e}) — giữ worker, "
                        f"không ngắt hàng loạt / không restart loop",
                        flush=True,
                    )
                else:
                    consec_other += 1
                    print(
                        f"[WS-POOL] Lỗi vòng WS (giữ worker): {e}",
                        flush=True,
                    )
                    if consec_other >= 5:
                        print(
                            f"[WS-POOL] Lỗi lặp {consec_other} lần — "
                            f"soft-restart worker",
                            flush=True,
                        )
                        raise
                with contextlib.suppress(Exception):
                    await sup.restart_dead_tasks()
                with contextlib.suppress(Exception):
                    await sup.restart_listener_if_dead(cfg)
                # sleep thất bại (loop chết) → không spin: raise
                try:
                    await asyncio.sleep(1.0 if transient else 2.0)
                except Exception as sleep_exc:
                    if stopping():
                        return
                    print(
                        f"[WS-POOL] sleep lỗi sau vòng WS ({sleep_exc}) — "
                        f"soft-restart",
                        flush=True,
                    )
                    raise

    try:
        # Lớp ngoài: 10038 nuốt giữ pool; loop chết → raise soft-restart.
        while not stopping():
            try:
                await _supervise_rounds()
                break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if stopping() or _is_ws_shutdown_error(e):
                    break
                if _is_event_loop_dead_error(e):
                    print(
                        f"[WS-POOL] Event loop chết (lớp ngoài): {e} — "
                        f"soft-restart",
                        flush=True,
                    )
                    raise
                if _is_transient_ws_loop_error(e):
                    print(
                        f"[WS-POOL] Socket thoát lớp ngoài ({e}) — "
                        f"nuốt, giữ pool (không soft-restart / không đóng WS)",
                        flush=True,
                    )
                    with contextlib.suppress(Exception):
                        await sup.restart_dead_tasks()
                    try:
                        await asyncio.sleep(1.0)
                    except Exception as sleep_exc:
                        if _is_event_loop_dead_error(sleep_exc):
                            raise
                        raise
                    continue
                print(
                    f"[WS-POOL] Lỗi lớp ngoài — soft-restart: {e}",
                    flush=True,
                )
                raise
    finally:
        if health_task is not None:
            health_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await health_task
        if progress_task is not None:
            progress_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await progress_task
        # Shutdown không được ném 10038 ra ngoài → soft-restart cả pool.
        with contextlib.suppress(Exception):
            await _graceful_ws_supervisor_shutdown(sup, timeout=12.0)
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
        _active_ws_supervisor = None
        _active_ws_loop = None


async def _managed_ws_loop_once(
    sup: WsPoolSupervisor,
    cfg: dict[str, Any],
    *,
    resync_on: bool,
    interval: float,
) -> None:
    """Một vòng wait/resync — tách ra để bắt WinError 10038 không thoát worker."""
    from xoso66_shutdown import stopping

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
    try:
        sleep_task = asyncio.create_task(asyncio.sleep(wait_sec))
    except RuntimeError as e:
        if _is_event_loop_dead_error(e):
            raise
        raise RuntimeError("no running event loop") from e
    pending = set(ws_tasks + listener_tasks + [sleep_task])
    done: set[asyncio.Task] = set()
    try:
        while pending and not stopping():
            finished, pending = await _safe_asyncio_wait(
                pending,
                timeout=1.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
            done |= finished
            if (
                _ws_pool_round_check.is_set()
                or _ws_pool_resync_check.is_set()
                or _ws_after_deposit_check.is_set()
            ):
                break
            if finished:
                for task in finished:
                    if task.cancelled():
                        continue
                    try:
                        exc = task.exception()
                    except asyncio.CancelledError:
                        continue
                    except Exception as e:
                        if _is_transient_ws_loop_error(e):
                            continue
                        raise
                    if exc and not isinstance(exc, asyncio.CancelledError):
                        if _is_transient_ws_loop_error(exc):
                            print(
                                f"[WS-POOL] Task {task.get_name() or '?'} "
                                f"socket: {exc}",
                                flush=True,
                            )
                        elif task is not sup.listener_task:
                            print(
                                f"[WS-POOL] Task {task.get_name() or '?'} "
                                f"lỗi: {exc}",
                                flush=True,
                            )
        if stopping():
            for t in pending:
                t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            _ws_pool_round_check.clear()
            _ws_pool_resync_check.clear()
            _ws_after_deposit_check.clear()
            return
        if _ws_after_deposit_check.is_set() and not stopping():
            _ws_after_deposit_check.clear()
            try:
                await sup.resync_from_config(
                    refresh_new=True, round_start=False
                )
            except Exception as e:
                if _is_transient_ws_loop_error(e):
                    print(
                        f"[WS-POOL] Resync sau nạp — socket tạm (bỏ qua, giữ pool): {e}",
                        flush=True,
                    )
                else:
                    print(f"[WS-POOL] Resync sau nạp lỗi: {e}", flush=True)
        ran_phiên_mới = False
        if _ws_pool_round_check.is_set() and not stopping():
            _ws_pool_round_check.clear()
            global _ws_round_resync_done, _ws_round_resync_pending
            pending_key: tuple[int, str] | None
            with _ws_round_resync_lock:
                pending_key = _ws_round_resync_pending
            try:
                await sup.resync_from_config(
                    refresh_new=False, round_start=True
                )
                ran_phiên_mới = True
                if pending_key is not None:
                    with _ws_round_resync_lock:
                        _ws_round_resync_done = pending_key
                        if _ws_round_resync_pending == pending_key:
                            _ws_round_resync_pending = None
            except Exception as e:
                if _is_transient_ws_loop_error(e):
                    print(
                        f"[WS-POOL] Resync đầu phiên — socket tạm (bỏ qua, giữ pool): {e}",
                        flush=True,
                    )
                else:
                    print(f"[WS-POOL] Resync đầu phiên lỗi: {e}", flush=True)
        if _ws_pool_resync_check.is_set() and not stopping():
            _ws_pool_resync_check.clear()
            # Vừa chạy Phiên mới trong cùng tick → bỏ Resync trùng.
            if not ran_phiên_mới:
                try:
                    await sup.resync_from_config(
                        refresh_new=False, round_start=False
                    )
                except Exception as e:
                    if _is_transient_ws_loop_error(e):
                        print(
                            f"[WS-POOL] Resync (evict/stale) — socket tạm "
                            f"(bỏ qua, giữ pool): {e}",
                            flush=True,
                        )
                    else:
                        print(
                            f"[WS-POOL] Resync (evict/stale) lỗi: {e}",
                            flush=True,
                        )
        elif sleep_task in done and resync_on and not stopping():
            try:
                await sup.resync_from_config(refresh_new=False)
            except Exception as e:
                if _is_transient_ws_loop_error(e):
                    print(
                        f"[WS-POOL] Resync định kỳ — socket tạm (bỏ qua, giữ pool): {e}",
                        flush=True,
                    )
                else:
                    print(f"[WS-POOL] Resync định kỳ lỗi: {e}", flush=True)
        if not stopping():
            try:
                await sup.restart_dead_tasks()
            except Exception as e:
                if _is_transient_ws_loop_error(e):
                    print(
                        f"[WS-POOL] restart_dead — socket tạm (giữ pool): {e}",
                        flush=True,
                    )
                else:
                    print(f"[WS-POOL] restart_dead lỗi: {e}", flush=True)
            try:
                await sup.prune_stale_unconnected_tasks(cfg)
            except Exception as e:
                if not _is_transient_ws_loop_error(e):
                    print(f"[WS-POOL] prune_stale lỗi: {e}", flush=True)
            try:
                await sup.restart_listener_if_dead(cfg)
            except Exception as e:
                if not _is_transient_ws_loop_error(e):
                    print(f"[WS-POOL] restart_listener lỗi: {e}", flush=True)
    finally:
        if sleep_task in pending or sleep_task in done or not sleep_task.done():
            sleep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sleep_task


async def _graceful_ws_supervisor_shutdown(
    sup: Any, *, timeout: float = 12.0
) -> None:
    if sup is None:
        return
    try:
        await asyncio.wait_for(sup.shutdown(), timeout=timeout)
    except Exception:
        pass


def _close_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Đóng loop sau supervisor shutdown — tránh GeneratorExit hàng loạt."""
    global _active_ws_supervisor, _active_ws_loop
    if loop.is_closed():
        _active_ws_supervisor = None
        _active_ws_loop = None
        asyncio.set_event_loop(None)
        return
    try:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            # Không bọc wait_for: nếu loop hỏng giữa chừng, wait_for bị
            # _ready.clear() → RuntimeWarning "coroutine never awaited".
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
    except Exception:
        pass
    try:
        if not loop.is_closed():
            loop.run_until_complete(loop.shutdown_asyncgens())
    except Exception:
        pass
    with contextlib.suppress(Exception):
        loop.close()
    asyncio.set_event_loop(None)
    _active_ws_supervisor = None
    _active_ws_loop = None


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


def _run_ws_worker_once(
    account_ids: list[str] | None = None,
    *,
    ws_count: int = WS_WORKER_COUNT,
    refresh_before_connect: bool = True,
) -> None:
    from xoso66_config_util import load_config
    from xoso66_shutdown import stopping
    from xoso66_ws_pool import ws_pool_resync_enabled

    if stopping():
        return

    cfg = load_config()
    ids = account_ids

    async def _main() -> None:
        if ws_pool_resync_enabled(cfg):
            await run_managed_ws_workers(
                ids,
                refresh_before_connect=refresh_before_connect,
            )
        else:
            await run_dual_ws_workers(
                ids,
                ws_count=ws_count or ws_account_count(cfg),
                refresh_before_connect=refresh_before_connect,
            )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _install_loop_exception_handler(loop)
    sup_ref: Any = None
    crash_snap: list[str] = []
    try:
        loop.run_until_complete(_main())
    except BaseException as e:
        if not isinstance(e, Exception):
            raise
        # Snapshot TRƯỚC khi finally đóng task — tránh ghi đè list rỗng.
        with contextlib.suppress(Exception):
            live_sup = _active_ws_supervisor
            if live_sup is not None:
                crash_snap = [
                    str(a).strip()
                    for a in (getattr(live_sup, "tasks", {}) or {})
                    if str(a).strip()
                ]
                if crash_snap:
                    remember_ws_pool_snapshot(crash_snap)
        if _is_transient_ws_loop_error(e):
            print(
                f"[WS-WORKER] Loop socket thoát (_main): {e} — "
                f"soft-reconnect list nick (hiếm; lỗi đã nuốt trong vòng quản lý)",
                flush=True,
            )
        sup_ref = _active_ws_supervisor
        raise
    finally:
        if crash_snap:
            remember_ws_pool_snapshot(crash_snap)
        elif not get_last_ws_pool_snapshot():
            with contextlib.suppress(Exception):
                live_sup = sup_ref or _active_ws_supervisor
                if live_sup is not None:
                    ids = [
                        str(a).strip()
                        for a in (getattr(live_sup, "tasks", {}) or {})
                        if str(a).strip()
                    ]
                    if ids:
                        remember_ws_pool_snapshot(ids)
        sup_ref = sup_ref or _active_ws_supervisor
        if sup_ref is not None and not loop.is_closed():
            shut_coro = None
            try:
                shut_coro = _graceful_ws_supervisor_shutdown(sup_ref)
                loop.run_until_complete(shut_coro)
                shut_coro = None
            except Exception:
                pass
            finally:
                if shut_coro is not None:
                    shut_coro.close()
        _close_event_loop(loop)


def run_ws_worker_blocking(
    account_ids: list[str] | None = None,
    *,
    ws_count: int = WS_WORKER_COUNT,
    refresh_before_connect: bool = True,
) -> None:
    """
    Chạy WS pool — tự restart khi crash (24/7).
    Lỗi socket tạm (WinError 10038…): không escalate backoff; ưu tiên reconnect pool cũ.
    Thoát sạch khi request_stop() / Ctrl+C.
    """
    from xoso66_config_util import load_config
    from xoso66_shutdown import sleep_interruptible, stopping
    from xoso66_ws_pool import reset_ws_pool_runtime_state

    cfg = load_config()
    initial_backoff = _game_worker_float(cfg, "ws_worker_restart_backoff_sec", 5.0)
    backoff = initial_backoff
    max_backoff = max(
        backoff,
        _game_worker_float(cfg, "ws_worker_restart_backoff_max_sec", 120.0),
    )
    attempt = 0
    last_pool_ids: list[str] = [
        str(x).strip() for x in (account_ids or []) if str(x).strip()
    ]
    if last_pool_ids:
        remember_ws_pool_snapshot(last_pool_ids)
    # Sau crash socket: lần mở lại không ép refresh token (nhanh hơn nhiều).
    next_refresh = bool(refresh_before_connect)

    while not stopping():
        attempt += 1
        snap = get_last_ws_pool_snapshot()
        resume_ids = snap or list(last_pool_ids)
        do_refresh = next_refresh
        next_refresh = bool(refresh_before_connect)
        if attempt > 1:
            if snap:
                last_pool_ids = snap
                resume_ids = snap
            # Snapshot quá mỏng sau crash → lấy lại list Đang Chơi (đúng ý: giữ list, chỉ mở WS).
            with contextlib.suppress(Exception):
                from xoso66_ws_pool import dang_choi_account_ids, ws_account_count

                dang = [
                    a
                    for a in dang_choi_account_ids(cfg)
                    if str(a).strip()
                ][: ws_account_count(cfg)]
                if dang and len(resume_ids) < max(3, len(dang) // 2):
                    print(
                        f"[WS-WORKER] Soft restart — snapshot {len(resume_ids)} quá mỏng, "
                        f"resume {len(dang)} Đang Chơi",
                        flush=True,
                    )
                    resume_ids = dang
                    last_pool_ids = dang
                    remember_ws_pool_snapshot(dang)
            reset_ws_pool_runtime_state()
            cancel_ws_pool_pending_work()
        try:
            _run_ws_worker_once(
                resume_ids or account_ids,
                ws_count=ws_count,
                refresh_before_connect=do_refresh,
            )
            break
        except KeyboardInterrupt:
            break
        except Exception as e:
            if stopping():
                break
            # Snapshot trước khi loop chết hẳn (supervisor có thể còn).
            with contextlib.suppress(Exception):
                if _active_ws_supervisor is not None:
                    remember_ws_pool_snapshot(
                        list(getattr(_active_ws_supervisor, "tasks", {}) or {})
                    )
            if _is_ws_shutdown_error(e):
                if not sleep_interruptible(2.0):
                    break
                continue
            if _is_transient_ws_loop_error(e) or _is_event_loop_dead_error(e):
                wait = min(5.0, max(2.0, initial_backoff))
                n_keep = len(get_last_ws_pool_snapshot() or resume_ids)
                why = "loop chết" if _is_event_loop_dead_error(e) else "socket"
                print(
                    f"[WS-WORKER] Crash ({why}): {e} — soft restart "
                    f"sau {wait:.0f}s (giữ ~{n_keep} nick, không ép refresh, lần {attempt})",
                    flush=True,
                )
                if not sleep_interruptible(wait):
                    break
                backoff = initial_backoff
                next_refresh = False
                continue
            print(
                f"[WS-WORKER] Crash (lỗi): {e} — "
                f"mở lại sau {backoff:.0f}s (lần {attempt})",
                flush=True,
            )
            if not sleep_interruptible(backoff):
                break
            backoff = min(max_backoff, backoff * 1.5)


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
