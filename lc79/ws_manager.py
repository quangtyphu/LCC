"""Quản lý WS tài xỉu: một nick một socket, chờ sau disconnect, không reconnect ngay."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from constants import active_ws

RECONNECT_BASE_DELAY_S = 20.0
RECONNECT_MAX_DELAY_S = 60.0
RECONNECT_STEP_S = 10.0
CIRCUIT_WINDOW_S = 300.0
CIRCUIT_MAX_IN_WINDOW = 5
CIRCUIT_PAUSE_S = 60.0
CLEANUP_WAIT_TIMEOUT_S = 8.0
POST_CLOSE_BUFFER_S = 0.5

_user_locks: dict[str, asyncio.Lock] = {}
_reconnect_state: dict[str, dict[str, Any]] = {}
_pending_reconnect: dict[str, asyncio.Task] = {}


def _lock(user: str) -> asyncio.Lock:
    return _user_locks.setdefault(user, asyncio.Lock())


def is_ws_alive(user: str) -> bool:
    """WS đang mở và task handle_ws còn chạy."""
    entry = active_ws.get(user)
    if not entry:
        return False
    task = entry.get("task")
    if not task or task.done():
        return False
    return bool(entry.get("ws_connected_at"))


def _state(user: str) -> dict[str, Any]:
    return _reconnect_state.setdefault(
        user,
        {
            "reconnect_times": [],
            "current_backoff": RECONNECT_BASE_DELAY_S,
            "circuit_open_until": 0.0,
        },
    )


def notify_ws_connected(user: str) -> None:
    """Gọi sau khi websockets.connect + authorize thành công."""
    st = _state(user)
    st["current_backoff"] = RECONNECT_BASE_DELAY_S
    st["circuit_open_until"] = 0.0


def cancel_pending_reconnect(user: str) -> None:
    task = _pending_reconnect.pop(user, None)
    if task and not task.done():
        task.cancel()


def compute_reconnect_delay(user: str) -> float:
    now = time.time()
    st = _state(user)
    times: list[float] = st["reconnect_times"]
    st["reconnect_times"] = [t for t in times if now - t < CIRCUIT_WINDOW_S]

    open_until = float(st.get("circuit_open_until") or 0)
    if open_until > now:
        return open_until - now

    if len(st["reconnect_times"]) >= CIRCUIT_MAX_IN_WINDOW:
        st["circuit_open_until"] = now + CIRCUIT_PAUSE_S
        st["reconnect_times"] = []
        st["current_backoff"] = RECONNECT_BASE_DELAY_S
        print(
            f"🛑 [{user}] Circuit OPEN — ≥{CIRCUIT_MAX_IN_WINDOW} reconnect/"
            f"{int(CIRCUIT_WINDOW_S)}s → nghỉ {int(CIRCUIT_PAUSE_S)}s",
            flush=True,
        )
        return CIRCUIT_PAUSE_S

    delay = float(st.get("current_backoff") or RECONNECT_BASE_DELAY_S)
    st["current_backoff"] = min(delay + RECONNECT_STEP_S, RECONNECT_MAX_DELAY_S)
    st["reconnect_times"].append(now)
    return delay


async def wait_until_not_in_active_ws(user: str, timeout: float = CLEANUP_WAIT_TIMEOUT_S) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if user not in active_ws:
            return True
        entry = active_ws.get(user)
        task = entry.get("task") if entry else None
        if task is None or task.done():
            if user in active_ws:
                active_ws.pop(user, None)
            return True
        await asyncio.sleep(0.1)
    return user not in active_ws


async def close_user_ws_fully(user: str) -> None:
    """Cancel task, chờ handle_ws dọn active_ws, buffer ngắn."""
    from ws_connection import disconnect_user

    cancel_pending_reconnect(user)
    await disconnect_user(user)
    await wait_until_not_in_active_ws(user)
    await asyncio.sleep(POST_CLOSE_BUFFER_S)


async def _ensure_ws_unlocked(acc: dict, *, reason: str = "") -> bool:
    user = acc["username"]
    if is_ws_alive(user):
        print(f"⏭️ [{user}] WS đang mở — bỏ qua connect ({reason})", flush=True)
        return False

    entry = active_ws.get(user)
    if entry:
        task = entry.get("task")
        if task and not task.done():
            print(f"⏭️ [{user}] Task WS đang chạy — bỏ qua connect ({reason})", flush=True)
            return False

    from ws_connection import handle_ws

    q = asyncio.Queue()
    conn_id = uuid.uuid4().hex
    active_ws[user] = {
        "queue": q,
        "task": None,
        "acc": acc,
        "conn_id": conn_id,
    }
    task = asyncio.create_task(handle_ws(acc, conn_id))
    active_ws[user]["task"] = task
    if reason:
        print(f"🔗 [{user}] Mở WS ({reason}) conn={conn_id[:8]}", flush=True)
    return True


async def ensure_ws_for_user(acc: dict, *, reason: str = "") -> bool:
    """Mở WS nếu chưa có. Trả False nếu đã sống hoặc đang connect."""
    user = acc["username"]
    async with _lock(user):
        if is_ws_alive(user):
            return False
        entry = active_ws.get(user)
        if entry:
            task = entry.get("task")
            if task and not task.done():
                return False
            await close_user_ws_fully(user)
        return await _ensure_ws_unlocked(acc, reason=reason or "ensure")


async def replace_ws_connection(acc: dict, *, reason: str = "") -> bool:
    """Đóng hẳn WS cũ → chờ 20s+ (backoff) → mở mới."""
    user = acc["username"]
    async with _lock(user):
        had_connection = user in active_ws or is_ws_alive(user)
        await close_user_ws_fully(user)
        if had_connection:
            delay = compute_reconnect_delay(user)
            n = len(_state(user)["reconnect_times"])
            print(
                f"⏳ [{user}] Đã đóng WS — chờ {delay:.0f}s trước khi mở lại "
                f"({n}/{CIRCUIT_MAX_IN_WINDOW} trong {int(CIRCUIT_WINDOW_S)}s) [{reason}]",
                flush=True,
            )
            await asyncio.sleep(delay)
        return await _ensure_ws_unlocked(acc, reason=reason or "replace")


def schedule_reconnect_after_drop(
    user: str,
    acc: dict,
    exit_reason: str,
    *,
    connected_at: float | None = None,
) -> None:
    """Sau khi handle_ws đóng socket — chờ rồi mới connect lại (không ngay)."""
    uptime = (time.time() - connected_at) if connected_at else 0.0
    print(
        f"⚠️ [{user}] WS ngắt: {exit_reason} | sống {uptime:.1f}s",
        flush=True,
    )

    async def _worker() -> None:
        try:
            delay = compute_reconnect_delay(user)
            n = len(_state(user)["reconnect_times"])
            print(
                f"⏳ [{user}] Chờ {delay:.0f}s trước khi reconnect "
                f"({n}/{CIRCUIT_MAX_IN_WINDOW} trong {int(CIRCUIT_WINDOW_S)}s)",
                flush=True,
            )
            await asyncio.sleep(delay)
            if is_ws_alive(user):
                return
            async with _lock(user):
                if is_ws_alive(user):
                    return
                await _ensure_ws_unlocked(acc, reason=f"reconnect:{exit_reason}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"❌ [{user}] Lỗi reconnect worker: {e}", flush=True)
        finally:
            _pending_reconnect.pop(user, None)

    cancel_pending_reconnect(user)
    _pending_reconnect[user] = asyncio.create_task(_worker())


def ws_debug_snapshot() -> dict[str, Any]:
    now = time.time()
    out: dict[str, Any] = {}
    for user, entry in active_ws.items():
        connected_at = entry.get("ws_connected_at")
        st = _reconnect_state.get(user, {})
        times = [t for t in st.get("reconnect_times", []) if now - t < CIRCUIT_WINDOW_S]
        out[user] = {
            "conn_id": (entry.get("conn_id") or "")[:12],
            "connected": is_ws_alive(user),
            "uptime_s": round(now - connected_at, 1) if connected_at else None,
            "task_done": bool(entry.get("task") and entry["task"].done()),
            "reconnects_5m": len(times),
            "circuit_open": float(st.get("circuit_open_until") or 0) > now,
            "pending_reconnect": user in _pending_reconnect,
        }
    return out
