# -*- coding: utf-8 -*-
"""
WebSocket mini-game XOSO66 (không dùng LC79 / Socket.IO).

  python xoso66_minigame_ws.py
  python xoso66_minigame_ws.py -u quangtyphu
  python xoso66_minigame_ws.py -a acc1 --duration 120

Giữ kết nối: subscribe + ping/pong. In jackpot, kết quả Tài/Xỉu, phiên mới (game_id catalog).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import json
import os
import random
import socket
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import urlparse

import socks

from xoso66_deposit import DEFAULT_UA
from xoso66_minigame_catalog import DEFAULT_JACKPOT_GAME_IDS, GAME_ID_LABELS
from xoso66_minigame_http import MINIGAME_BASE, get_minigame, ws_url_from_token

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

WS_HOST = urlparse(
    os.environ.get("XOSO66_MINIGAME_WS_BASE", "wss://wss-minigame-viet.227290.com")
).netloc or "wss-minigame-viet.227290.com"
_WS_TCP_HOST_CACHE: str | None = None
_WS_TCP_HOST_CACHE_AT = 0.0
_WS_TCP_HOST_CACHE_LOCK = threading.Lock()
_WS_TCP_HOST_CACHE_SEC = 300.0
# Domain parking của Above.com: TLS vẫn thành công nhưng trả HTML HTTP 200,
# không phải WebSocket 101. Ngày 2026-09-25 A record bị trỏ nhầm vào đây,
# trong khi AAAA vẫn trỏ Cloudflare và web qua IPv6 vẫn chơi được.
_WS_PARKING_IPV4 = frozenset({"103.224.212.141"})
_WS_CLOUDFLARE_FALLBACK_IPV4 = ("104.18.12.214", "104.18.13.214")
_WS_BROKEN_A_RECORD_HOSTS = frozenset({"wss-minigame-viet.227290.com"})

# game_id trong subscribe — lobby 0; jackpot: 9,17,18,19,2 (bỏ game 4 — không hũ)
# Đủ 5 game: "0,9,[17,18,19,2]"
DEFAULT_WS_SUBSCRIBE = os.environ.get("XOSO66_WS_SUBSCRIBE", "0,9,[17,18,19,2]")
DEFAULT_WATCH_GAME_IDS = frozenset(DEFAULT_JACKPOT_GAME_IDS)
WS_PING_INTERVAL_SEC = float(os.environ.get("XOSO66_WS_PING_INTERVAL", "20"))
# 1 nick = 1 WS, subscribe mọi game hũ trên cùng socket (0 + 9,17,18,19,2).
# Không nhận open_info (phiên) game nào trong khoảng này → token mới + subscribe lại.
# Ping/hũ lobby vẫn tới thì idle 120s không bắt được socket chết kênh phiên.
WS_OPEN_INFO_STALE_SEC = float(os.environ.get("XOSO66_WS_OPEN_INFO_STALE_SEC", "90"))
# next_info snapshot lúc reconnect / queue đầy: còn ít hơn mức này thì không cược / không 5 việc.
NEXT_INFO_SKIP_IF_END_LEFT_SEC = 2.0
# Handshake SOCKS+TLS sau khi đã có token. Chờ hàng cửa không tính vào đây.
WS_CONNECT_BUDGET_SEC = float(os.environ.get("XOSO66_WS_CONNECT_BUDGET_SEC", "20"))
# getToken (HTTP+proxy) riêng — không chia chung 20s với TLS.
WS_TOKEN_BUDGET_SEC = float(os.environ.get("XOSO66_WS_TOKEN_BUDGET_SEC", "45"))
# Chờ semaphore mở WS — 40 nick / batch 4 không bị cắt 20s rồi cancel handshake.
WS_CONNECT_SLOT_WAIT_SEC = float(os.environ.get("XOSO66_WS_CONNECT_SLOT_WAIT_SEC", "90"))
SOCKS_CONNECT_TIMEOUT_SEC = float(os.environ.get("XOSO66_SOCKS_CONNECT_TIMEOUT_SEC", "8"))
WS_SUBSCRIBE_TIMEOUT_SEC = float(os.environ.get("XOSO66_WS_SUBSCRIBE_TIMEOUT_SEC", "8"))

_OPEN_INFO_RX: dict[int, float] = {}
_OPEN_INFO_RX_LOCK = threading.Lock()
_WS_INGRESS_HEALTH: dict[str, dict[str, float]] = {}
_WS_INGRESS_HEALTH_LOCK = threading.Lock()
_LAST_NEXT_INFO: dict[str, Any] = {}
_LAST_NEXT_INFO_LOCK = threading.Lock()
def note_claimed_next_info(next_info: dict[str, Any] | None) -> None:
    """Ghi begin/end phiên đang chạy — spawn_cap / còn cửa, không chặn đóng-mở WS."""
    if not isinstance(next_info, dict):
        return
    with _LAST_NEXT_INFO_LOCK:
        _LAST_NEXT_INFO.clear()
        _LAST_NEXT_INFO.update(next_info)


def _copied_last_next_info() -> dict[str, Any] | None:
    with _LAST_NEXT_INFO_LOCK:
        return dict(_LAST_NEXT_INFO) if _LAST_NEXT_INFO else None


def ws_socket_churn_blocked() -> tuple[bool, str]:
    """Không còn cửa begin/end. Đóng/mở WS theo Đang Chơi + A, không theo đồng hồ phiên."""
    return False, ""


def current_round_still_open() -> bool:
    left = next_info_end_left_sec(_copied_last_next_info())
    return left is not None and left > 0


def note_open_info_received(game_id: int, *, received_wall: float | None = None) -> None:
    """Mốc raw open_info tới socket — không dùng giờ handler xử lý."""
    gid = int(game_id or 0)
    if gid <= 0:
        return
    with _OPEN_INFO_RX_LOCK:
        _OPEN_INFO_RX[gid] = float(received_wall or time.time())


def newest_open_info_rx() -> tuple[int | None, float | None]:
    """(game_id, age_sec) của open_info mới nhất — mọi game trên mọi nick."""
    with _OPEN_INFO_RX_LOCK:
        if not _OPEN_INFO_RX:
            return None, None
        gid, ts = max(_OPEN_INFO_RX.items(), key=lambda kv: kv[1])
    return int(gid), max(0.0, time.time() - ts)


def clear_open_info_rx() -> None:
    with _OPEN_INFO_RX_LOCK:
        _OPEN_INFO_RX.clear()


def format_ws_open_info_health(game_id: int | None = None) -> str:
    """Nhãn health: open_info=12s | open_info=STALE 180s | open_info=chưa."""
    _ = game_id
    gid, age = newest_open_info_rx()
    if gid is None or age is None:
        return "open_info=chưa"
    stale = WS_OPEN_INFO_STALE_SEC > 0 and age > WS_OPEN_INFO_STALE_SEC
    tag = "STALE " if stale else ""
    return f"open_info={tag}{age:.0f}s"


def note_ws_ingress_health(
    account_id: str,
    router: "WsInboundRouter",
    *,
    received_wall: float | None = None,
    queue_age_sec: float | None = None,
) -> None:
    aid = str(account_id or "").strip()
    if not aid:
        return
    with _WS_INGRESS_HEALTH_LOCK:
        row = _WS_INGRESS_HEALTH.setdefault(aid, {})
        if received_wall is not None:
            row["last_recv_wall"] = float(received_wall)
        if queue_age_sec is not None:
            row["last_queue_age_sec"] = max(0.0, float(queue_age_sec))
            row["max_queue_age_sec"] = max(
                float(row.get("max_queue_age_sec") or 0),
                max(0.0, float(queue_age_sec)),
            )
        row["critical_depth"] = float(router.critical_depth())
        row["bulk_depth"] = float(router.bulk_depth())
        row["bulk_coalesced"] = float(router.bulk_coalesced)
        row["critical_dropped"] = float(router.critical_dropped)


def format_ws_ingress_health(account_id: str) -> str:
    aid = str(account_id or "").strip()
    with _WS_INGRESS_HEALTH_LOCK:
        row = dict(_WS_INGRESS_HEALTH.get(aid) or {})
    if not row:
        return "ingress=chưa"
    recv_age = max(0.0, time.time() - float(row.get("last_recv_wall") or time.time()))
    return (
        f"ingress_recv={recv_age:.0f}s "
        f"q={int(row.get('critical_depth') or 0)}/"
        f"{int(row.get('bulk_depth') or 0)} "
        f"q_age={float(row.get('last_queue_age_sec') or 0):.3f}s "
        f"q_max={float(row.get('max_queue_age_sec') or 0):.3f}s "
        f"coal={int(row.get('bulk_coalesced') or 0)} "
        f"dropC={int(row.get('critical_dropped') or 0)}"
    )

# Gợi ý nhận diện jackpot / pool trong payload JSON
_JACKPOT_TYPE_HINTS = frozenset(
    {
        "jackpot",
        "jackpots",
        "jackpot_update",
        "jackpot_money",
        "jackpot_pool",
        "pool",
        "prize_pool",
        "pool_update",
        "grand_pool",
    }
)
_JACKPOT_FIELD_HINTS = frozenset(
    {
        "jackpot",
        "jackpots",
        "jackpot_money",
        "jackpot_amount",
        "money",
        "pool",
        "pool_money",
        "prize_pool",
        "grand_prize",
        "total_jackpot",
    }
)


@dataclass
class JackpotState:
    """Jackpot theo game_id (cập nhật khi WS push)."""

    by_game: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_raw: list[dict[str, Any]] = field(default_factory=list)
    msg_count: int = 0
    jackpot_updates: int = 0


@dataclass(frozen=True)
class WsInboundItem:
    """Một frame đã decode kèm đúng mốc nhận từ socket."""

    obj: Any
    received_mono: float
    received_wall: float


class WsInboundRouter:
    """
    Critical frame không đứng sau jackpot/game_info backlog.

    Bulk frame được coalesce theo (type, game_id), chỉ giữ bản mới nhất.
    """

    _CRITICAL_TYPES = frozenset(
        {
            "g_open_info",
            "open_info",
            "balance",
            "g_balance",
            "logout",
            "ping",
            "heartbeat",
            "heart",
        }
    )

    def __init__(self, *, critical_maxsize: int = 64) -> None:
        self._critical: asyncio.Queue[WsInboundItem] = asyncio.Queue(
            maxsize=max(8, int(critical_maxsize))
        )
        self._bulk: dict[tuple[str, int], WsInboundItem] = {}
        self._wake = asyncio.Event()
        self._closed = False
        self.bulk_coalesced = 0
        self.critical_dropped = 0
        self.max_critical_age_sec = 0.0

    @staticmethod
    def _type_gid(obj: Any) -> tuple[str, int]:
        if not isinstance(obj, dict):
            return "", 0
        msg_type = str(
            obj.get("type") or obj.get("cmd") or obj.get("action") or ""
        ).lower()
        data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
        try:
            gid = int(data.get("game_id") or data.get("id") or 0)
        except (TypeError, ValueError):
            gid = 0
        return msg_type, gid

    @classmethod
    def _is_critical(cls, obj: Any) -> bool:
        if isinstance(obj, dict) and obj.get("_app_ping"):
            return True
        if _is_ping_payload(obj):
            return True
        msg_type, _gid = cls._type_gid(obj)
        if msg_type in cls._CRITICAL_TYPES:
            return True
        if msg_type in ("g_game_info", "game_info"):
            data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
            return data.get("is_open") == 1
        return False

    def put_raw(self, raw: str | bytes) -> WsInboundItem:
        item = WsInboundItem(
            obj=_decode_message(raw),
            received_mono=time.monotonic(),
            received_wall=time.time(),
        )
        if self._is_critical(item.obj):
            if self._critical.full():
                # Critical rất ít; nếu consumer chết thì giữ frame mới nhất.
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._critical.get_nowait()
                self.critical_dropped += 1
            self._critical.put_nowait(item)
        else:
            key = self._type_gid(item.obj)
            if key in self._bulk:
                self.bulk_coalesced += 1
            self._bulk[key] = item
        self._wake.set()
        return item

    def close(self) -> None:
        self._closed = True
        self._wake.set()

    def critical_depth(self) -> int:
        return self._critical.qsize()

    def bulk_depth(self) -> int:
        return len(self._bulk)

    async def get(self) -> WsInboundItem | None:
        while True:
            with contextlib.suppress(asyncio.QueueEmpty):
                item = self._critical.get_nowait()
                age = max(0.0, time.monotonic() - item.received_mono)
                self.max_critical_age_sec = max(self.max_critical_age_sec, age)
                return item
            if self._bulk:
                _key, item = self._bulk.popitem()
                return item
            if self._closed:
                return None
            self._wake.clear()
            if not self._critical.empty() or self._bulk or self._closed:
                continue
            await self._wake.wait()


_ROUND_START_HANDLERS: list[Any] = []
_ROUND_RESULT_HANDLERS: list[Any] = []
_RESULT_LOG_KEYS: set[str] = set()
_RESULT_LOG_LOCK = threading.Lock()
_START_CLAIM_KEYS: set[str] = set()
_START_CLAIM_LOCK = threading.Lock()


def _note_issue_once(store: set[str], lock: threading.Lock, game_id: int, issue: str) -> bool:
    issue_s = str(issue or "").strip()
    if not issue_s:
        return False
    key = f"{int(game_id)}:{issue_s}"
    with lock:
        if key in store:
            return False
        store.add(key)
        if len(store) > 200:
            old = list(store)[:100]
            for item in old:
                store.discard(item)
        return True


def note_round_result_logged(game_id: int, issue: str) -> bool:
    """True nếu lần đầu in KQ issue này — tránh in trùng watch + settlement."""
    return _note_issue_once(_RESULT_LOG_KEYS, _RESULT_LOG_LOCK, game_id, issue)


def note_round_start_claimed(game_id: int, issue: str) -> bool:
    """True nếu lần đầu claim phiên mới — mọi coordinator / socket dùng chung."""
    return _note_issue_once(_START_CLAIM_KEYS, _START_CLAIM_LOCK, game_id, issue)

_ROUND_OPEN_LOCK = threading.Lock()
_ROUND_OPEN_MONO: dict[tuple[int, str], float] = {}
_ROUND_OPEN_EVENTS: dict[tuple[int, str], threading.Event] = {}


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def note_round_bet_open_at(
    game_id: int, issue: str, open_mono: float, *, only_if_earlier: bool = True
) -> None:
    """Ghi monotonic lúc mở cửa dự kiến/thực — giữ mốc sớm nhất."""
    gid = int(game_id)
    issue_s = str(issue or "").strip()
    if not issue_s:
        return
    key = (gid, issue_s)
    with _ROUND_OPEN_LOCK:
        cur = _ROUND_OPEN_MONO.get(key)
        if cur is not None and only_if_earlier and open_mono >= cur:
            return
        if cur is None or open_mono < cur:
            _ROUND_OPEN_MONO[key] = open_mono
        ev = _ROUND_OPEN_EVENTS.get(key)
        if ev is None:
            ev = threading.Event()
            _ROUND_OPEN_EVENTS[key] = ev
        ev.set()
        if len(_ROUND_OPEN_MONO) > 200:
            for old in list(_ROUND_OPEN_MONO.keys())[:-100]:
                _ROUND_OPEN_MONO.pop(old, None)
                _ROUND_OPEN_EVENTS.pop(old, None)


def note_round_bet_open(game_id: int, issue: str) -> None:
    """Ghi lúc nhận game_info is_open=1 — không ghi đè mốc sớm hơn từ wait_countdown."""
    note_round_bet_open_at(game_id, issue, time.monotonic(), only_if_earlier=True)


def get_round_open_mono(game_id: int, issue: str) -> float | None:
    """Monotonic lúc mở cửa (sớm nhất đã biết) hoặc None."""
    key = (int(game_id), str(issue or "").strip())
    if not key[1]:
        return None
    with _ROUND_OPEN_LOCK:
        return _ROUND_OPEN_MONO.get(key)


def wait_for_round_bet_open(
    game_id: int, issue: str, timeout_sec: float
) -> float | None:
    """Chờ is_open=1; trả monotonic lúc mở cửa hoặc None nếu hết timeout."""
    gid = int(game_id)
    issue_s = str(issue or "").strip()
    if not issue_s:
        return None
    key = (gid, issue_s)
    with _ROUND_OPEN_LOCK:
        if key in _ROUND_OPEN_MONO:
            return _ROUND_OPEN_MONO[key]
        ev = _ROUND_OPEN_EVENTS.get(key)
        if ev is None:
            ev = threading.Event()
            _ROUND_OPEN_EVENTS[key] = ev
    if ev.wait(timeout=max(0.1, float(timeout_sec))):
        with _ROUND_OPEN_LOCK:
            return _ROUND_OPEN_MONO.get(key)
    return None


def register_round_start_handler(
    fn: Any,
) -> None:
    """Đăng ký callback (game_id, issue, next_info, reporter=...) khi BẮT ĐẦU PHIÊN."""
    if fn not in _ROUND_START_HANDLERS:
        _ROUND_START_HANDLERS.append(fn)


def register_round_result_handler(
    fn: Any,
) -> None:
    """Callback (game_id, issue, open_data, reporter=...) khi KẾT QUẢ phiên."""
    if fn not in _ROUND_RESULT_HANDLERS:
        _ROUND_RESULT_HANDLERS.append(fn)


class WsBroadcastCoordinator:
    """
    Nhiều WS, một lần báo / lưu mỗi sự kiện — giống LC79 ``new-session`` + ``session_seen``:

    - Mọi acc giữ WS (dự phòng khi rớt).
    - Nick nào nhận gói trước ``claim`` được → xử lý + in log; nick sau im lặng.
    - Ghi ``minigame_jackpots.json`` / BẮT ĐẦU / KẾT QUẢ: cùng quy tắc claim (không phân nick chính/phụ).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._reporter: dict[str, str] = {}
        self._latest_issue: dict[tuple[str, int], str] = {}

    def _claim(self, key: str, reporter: str = "") -> bool:
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            self._seen_order.append(key)
            if reporter:
                self._reporter[key] = reporter
            while len(self._seen_order) > 2000:
                old = self._seen_order.popleft()
                self._seen.discard(old)
                self._reporter.pop(old, None)
            return True

    def _claim_issue(
        self, kind: str, game_id: int, issue: str, reporter: str = ""
    ) -> bool:
        issue_s = str(issue or "").strip()
        if not issue_s:
            return False
        gid = int(game_id)
        key = f"{kind}:{gid}:{issue_s}"
        order_key = (kind, gid)
        with self._lock:
            latest = self._latest_issue.get(order_key)
            if latest is not None and issue_s <= latest:
                return False
            self._seen.add(key)
            self._seen_order.append(key)
            if latest is None or issue_s > latest:
                self._latest_issue[order_key] = issue_s
            if reporter:
                self._reporter[key] = reporter
            while len(self._seen_order) > 2000:
                old = self._seen_order.popleft()
                self._seen.discard(old)
                self._reporter.pop(old, None)
            return True

    def reporter_of(self, key: str) -> str:
        with self._lock:
            return self._reporter.get(key) or ""

    def claim_jackpot(self, game_id: int, money: Any, *, reporter: str = "") -> bool:
        return self._claim(f"j:{int(game_id)}:{money}", reporter)

    def claim_round_start(self, game_id: int, issue: str, *, reporter: str = "") -> bool:
        return self._claim_issue("s", game_id, issue, reporter)

    def claim_round_result(self, game_id: int, issue: str, *, reporter: str = "") -> bool:
        return self._claim_issue("r", game_id, issue, reporter)

@dataclass
class MultiGameWatchState:
    """Theo dõi phiên / kết quả / hũ cho nhiều game_id."""

    watch_ids: frozenset[int]
    labels: dict[int, str] = field(default_factory=dict)
    last_issue: dict[int, str] = field(default_factory=dict)
    last_open: dict[int, str] = field(default_factory=dict)
    last_open_info_at: dict[int, float] = field(default_factory=dict)
    last_jackpot: dict[int, str] = field(default_factory=dict)
    msg_count: int = 0
    jackpot_store: Any = None
    ws_account: str = ""
    log_prefix: str = ""
    log_game_info: bool = False
    broadcast: WsBroadcastCoordinator | None = None
    focus_game_id: int | None = None
    full_watch: bool = True


def parse_watch_game_ids(spec: str) -> frozenset[int]:
    s = (spec or "").strip().lower()
    if not s or s in ("all", "default"):
        return frozenset(DEFAULT_WATCH_GAME_IDS)
    if s in ("5", "6", "full", "jackpot"):
        return frozenset(DEFAULT_WATCH_GAME_IDS)
    out: set[int] = set()
    for part in s.replace("[", "").replace("]", "").split(","):
        p = part.strip()
        if p.isdigit():
            out.add(int(p))
    return frozenset(out) if out else frozenset(DEFAULT_WATCH_GAME_IDS)


def _game_tag(gid: int, labels: dict[int, str]) -> str:
    name = labels.get(gid) or GAME_ID_LABELS.get(gid) or ""
    return f"game_id={gid} ({name})" if name else f"game_id={gid}"


def _fmt_money(val: Any) -> str:
    s = str(val or "").strip()
    if not s:
        return "—"
    try:
        n = float(s.replace(",", ""))
        if n >= 1_000_000_000:
            return f"{n:,.0f}"
        return f"{n:,.2f}".rstrip("0").rstrip(".")
    except ValueError:
        return s


def resolve_account_id(*, username: str = "", account_id: str = "") -> str:
    """-u username hoặc -a acc id → account id trong DB."""
    aid = (account_id or "").strip()
    user = (username or "").strip()
    if aid:
        return aid
    if not user:
        return ""
    from xoso66_accounts_db import get_account_by_username

    row = get_account_by_username(user)
    if not row:
        raise SystemExit(f"Không tìm thấy account username='{user}' trong DB.")
    sess = row.get("session_json") or {}
    if not row.get("proxy") and not (sess.get("proxy") if isinstance(sess, dict) else False):
        raise SystemExit(f"Account '{user}' ({row['id']}) chưa có proxy trong DB.")
    return str(row["id"])


def _watch_gid(data: dict[str, Any]) -> int:
    return int(data.get("game_id") or data.get("id") or 0)


def listener_covering_rounds() -> bool:
    """Listener còn sống và đã connect — player không tranh claim phiên."""
    try:
        from xoso66_minigame_ws_worker import listener_is_covering_rounds

        return bool(listener_is_covering_rounds())
    except Exception:
        return False


def _should_claim_round_events(state: MultiGameWatchState) -> bool:
    """Listener claim chính; player chỉ backup khi listener không còn phủ phiên."""
    if state.full_watch:
        return True
    return not listener_covering_rounds()


def _refresh_focus_game(state: MultiGameWatchState) -> int | None:
    """Game đang chơi (auto_bet) hoặc hũ cao nhất — lọc log BẮT ĐẦU PHIÊN / KẾT QUẢ."""
    try:
        from xoso66_config_util import load_config
        from xoso66_jackpot_picker import focus_game_id

        state.focus_game_id = focus_game_id(load_config())
    except Exception:
        state.focus_game_id = None
    return state.focus_game_id


def _focus_game_id_for_log(state: MultiGameWatchState) -> int | None:
    try:
        from xoso66_config_util import load_config
        from xoso66_jackpot_picker import focus_game_id

        return focus_game_id(load_config())
    except Exception:
        return state.focus_game_id


def _should_log_watch_game_focus(
    gid: int, state: MultiGameWatchState, *, kind: str = "result"
) -> bool:
    """Chỉ game đang chơi (auto_bet) hoặc hũ cao nhất."""
    del kind
    fid = _focus_game_id_for_log(state)
    if fid is None:
        return False
    return int(gid) == int(fid)


def _should_log_watch_game(gid: int, state: MultiGameWatchState) -> bool:
    """Chỉ game đang chơi (focus) — BẮT ĐẦU PHIÊN."""
    return _should_log_watch_game_focus(gid, state, kind="start")


def _should_log_watch_game_result(gid: int, state: MultiGameWatchState) -> bool:
    """Chỉ game đang chơi (focus) — KẾT QUẢ phiên."""
    return _should_log_watch_game_focus(gid, state, kind="result")


def _emit_round_result_log(gid: int, data: dict[str, Any], state: MultiGameWatchState) -> None:
    if not _should_log_watch_game_result(gid, state):
        return
    issue = str(data.get("issue") or "").strip()
    if not note_round_result_logged(gid, issue):
        return
    from xoso66_round_log import log_round_result_header
    from xoso66_ws_balance import open_data_to_dices, resolve_winning_side

    winning = resolve_winning_side(data)
    log_round_result_header(
        issue=issue,
        winning_side=winning,
        dices=open_data_to_dices(data) if winning else None,
    )


def _round_start_log_delay_sec() -> float:
    from xoso66_config_util import load_config

    cfg = load_config()
    gw = cfg.get("game_worker") if isinstance(cfg.get("game_worker"), dict) else {}
    ab = cfg.get("auto_bet") if isinstance(cfg.get("auto_bet"), dict) else {}
    if "round_start_log_delay_sec" in gw:
        return max(0.0, float(gw.get("round_start_log_delay_sec") or 0))
    if "round_start_log_delay_sec" in ab:
        return max(0.0, float(ab.get("round_start_log_delay_sec") or 0))
    return 0.0


def _jackpot_display_for_game(state: MultiGameWatchState, gid: int) -> str:
    store = state.jackpot_store
    if store is None:
        return "—"
    try:
        data = store.load()
        row = (data.get("by_game") or {}).get(str(int(gid))) or {}
        money = row.get("money")
        if money is not None:
            return _fmt_money(money)
    except Exception:
        pass
    return "—"


def _game_display_name(gid: int, state: MultiGameWatchState) -> str:
    return state.labels.get(gid) or GAME_ID_LABELS.get(gid) or f"game_id={gid}"


def _emit_round_start_log(
    gid: int, state: MultiGameWatchState, *, issue: str = ""
) -> None:
    if not _should_log_watch_game(gid, state):
        return
    # Auto-bet in BẮT ĐẦU PHIÊN sau bet_plan_after_sec (tránh trùng + đúng thứ tự sau KQ phiên trước).
    try:
        from xoso66_round_log import assign_bet_console_enabled

        if assign_bet_console_enabled():
            handlers_ok = False
            with contextlib.suppress(Exception):
                from xoso66_auto_bet import auto_bet_handlers_ready

                handlers_ok = bool(auto_bet_handlers_ready())
            if handlers_ok:
                return
    except Exception:
        pass
    from xoso66_round_log import log_round_start_line

    name = _game_display_name(gid, state)
    jp_money = 0.0
    store = state.jackpot_store
    if store is not None:
        try:
            row = (store.load().get("by_game") or {}).get(str(int(gid))) or {}
            jp_money = float(str(row.get("money") or "0").replace(",", ""))
        except (TypeError, ValueError):
            jp_money = 0.0
    min_jp = 0.0
    try:
        from xoso66_config_util import load_config
        from xoso66_jackpot_picker import min_jackpot_vnd

        cfg = load_config()
        ab = cfg.get("auto_bet")
        if isinstance(ab, dict) and ab.get("enabled"):
            min_jp = min_jackpot_vnd(cfg)
    except Exception:
        pass
    log_round_start_line(
        game_label=name,
        jackpot_vnd=jp_money,
        issue=issue,
        min_jackpot_vnd=min_jp if min_jp > 0 else None,
        game_id=int(gid),
    )


def _schedule_round_start_log(
    gid: int, state: MultiGameWatchState, *, issue: str = ""
) -> None:
    _refresh_focus_game(state)
    delay = _round_start_log_delay_sec()
    if delay <= 0:
        _emit_round_start_log(gid, state, issue=issue)
        return
    threading.Timer(
        delay,
        lambda g=gid, s=state, i=issue: _emit_round_start_log(g, s, issue=i),
    ).start()


def _print_watch_jackpot(data: dict[str, Any], state: MultiGameWatchState) -> bool:
    gid = _watch_gid(data)
    if gid not in state.watch_ids:
        return False
    money = data.get("money") if data.get("money") is not None else data.get("jackpot")
    try:
        from xoso66_jackpot_hit_notify import record_jackpot_pool

        record_jackpot_pool(gid, money)
    except Exception:
        pass
    key = f"{gid}:{money}"
    if state.last_jackpot.get(gid) == key:
        return False
    state.last_jackpot[gid] = key
    if state.broadcast is not None and not state.broadcast.claim_jackpot(
        gid, money, reporter=state.ws_account
    ):
        return True
    store = state.jackpot_store
    if store is not None:
        name = state.labels.get(gid) or GAME_ID_LABELS.get(gid) or ""
        changed = store.record(
            gid,
            money,
            game_name=name,
            ws_account=state.ws_account,
            group_id=data.get("group_id"),
        )
        _refresh_focus_game(state)
        if changed:
            try:
                from xoso66_config_util import load_config
                from xoso66_jackpot_picker import sync_auto_bet_jackpot_gate

                sync_auto_bet_jackpot_gate(load_config())
            except Exception:
                pass
    return True


def parse_minigame_wall_time(value: Any) -> datetime | None:
    s = str(value or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def next_info_begin_age_sec(next_info: dict[str, Any] | None) -> float | None:
    if not isinstance(next_info, dict):
        return None
    begin = parse_minigame_wall_time(next_info.get("begin_time"))
    if begin is None:
        return None
    return (datetime.now() - begin).total_seconds()


def next_info_end_left_sec(next_info: dict[str, Any] | None) -> float | None:
    if not isinstance(next_info, dict):
        return None
    end = parse_minigame_wall_time(next_info.get("end_time"))
    if end is None:
        return None
    return (end - datetime.now()).total_seconds()


def next_info_too_late_to_act(next_info: dict[str, Any] | None) -> bool:
    """Snapshot reconnect / queue đầy: phiên đã hết hoặc còn < 2s — đừng cược."""
    left = next_info_end_left_sec(next_info)
    return left is not None and left < NEXT_INFO_SKIP_IF_END_LEFT_SEC


def ws_pool_resync_delay_sec(
    next_info: dict[str, Any] | None, gw: dict[str, Any] | None = None
) -> float | None:
    """
    Giây từ now tới lúc chạy việc 1–5. None = bỏ issue này.
    Neo begin+18s (sau cửa cược); sát end thì bỏ.
    """
    gw = gw if isinstance(gw, dict) else {}
    try:
        after_begin = max(0.0, float(gw.get("ws_pool_resync_after_begin_sec") or 18))
    except (TypeError, ValueError):
        after_begin = 18.0
    try:
        min_before_end = max(0.0, float(gw.get("ws_pool_resync_min_before_end_sec") or 8))
    except (TypeError, ValueError):
        min_before_end = 8.0
    try:
        fallback = max(0.0, float(gw.get("ws_pool_resync_delay_after_round_sec") or 2))
    except (TypeError, ValueError):
        fallback = 2.0
    now = datetime.now()
    begin = parse_minigame_wall_time((next_info or {}).get("begin_time"))
    end = parse_minigame_wall_time((next_info or {}).get("end_time"))
    if begin is None:
        return fallback
    target = begin + timedelta(seconds=after_begin)
    if end is not None:
        latest = end - timedelta(seconds=min_before_end)
        if now >= latest:
            return None
        if target > latest:
            target = latest
    delay = (target - now).total_seconds()
    if delay < 0:
        return 0.0
    return delay


def _run_handlers_bg(
    name: str,
    handlers: list[Any],
    *args: Any,
    log_prefix: str = "",
    **kwargs: Any,
) -> None:
    """Chạy callback phiên ngoài vòng recv — tránh kẹt 12 socket chung 1 loop."""
    snapshot = list(handlers)
    if not snapshot:
        return

    def _run() -> None:
        for fn in snapshot:
            try:
                fn(*args, **kwargs)
            except TypeError:
                try:
                    fn(*args)
                except Exception as e:
                    print(f"{log_prefix}[AUTO] {name}: {e}", flush=True)
            except Exception as e:
                print(f"{log_prefix}[AUTO] {name}: {e}", flush=True)

    threading.Thread(target=_run, name=name, daemon=True).start()


def _print_watch_phiên_mới_from_next(
    gid: int,
    nxt: dict[str, Any],
    state: MultiGameWatchState,
    *,
    received_mono: float | None = None,
) -> bool:
    """Phiên mới = next_info trong open_info (~30s/phiên), không dùng game_info đổi issue."""
    issue = str(nxt.get("issue") or "")
    if not issue or state.last_issue.get(gid) == issue:
        return False
    state.last_issue[gid] = issue
    run_handlers = True
    if state.broadcast is not None:
        run_handlers = state.broadcast.claim_round_start(
            gid, issue, reporter=state.ws_account
        )
    if run_handlers and next_info_too_late_to_act(nxt):
        age = next_info_begin_age_sec(nxt)
        left = next_info_end_left_sec(nxt)
        age_s = f"{age:.1f}s sau begin" if age is not None else "không rõ begin"
        if left is None:
            left_s = "không rõ end"
        elif left >= 0:
            left_s = f"còn end {left:.1f}s"
        else:
            left_s = f"end hết {abs(left):.1f}s"
        print(
            f"{state.log_prefix}bỏ phiên {issue} — next_info trễ ({age_s}, {left_s}) "
            f"— không cược / không việc status",
            flush=True,
        )
        run_handlers = False
    if run_handlers and not note_round_start_claimed(gid, issue):
        run_handlers = False
    if run_handlers:
        note_claimed_next_info(nxt)
        reporter = state.ws_account or (
            state.broadcast.reporter_of(f"s:{gid}:{issue}") if state.broadcast else ""
        )
        nxt_payload = dict(nxt)
        nxt_payload["_claimed_at_mono"] = time.monotonic()
        if received_mono is not None:
            nxt_payload["_ws_received_at_mono"] = float(received_mono)
            nxt_payload["_ws_queue_delay_sec"] = max(
                0.0, time.monotonic() - float(received_mono)
            )
        _run_handlers_bg(
            f"ws-round-start-{issue}",
            _ROUND_START_HANDLERS,
            gid,
            issue,
            nxt_payload,
            reporter=reporter,
            log_prefix=state.log_prefix,
        )

    did_log = False
    if run_handlers and _should_log_watch_game(gid, state):
        _schedule_round_start_log(gid, state, issue=issue)
        did_log = True
    return run_handlers or did_log


def _print_watch_open_info(
    data: dict[str, Any],
    state: MultiGameWatchState,
    *,
    received_mono: float | None = None,
    received_wall: float | None = None,
) -> bool:
    gid = _watch_gid(data)
    if gid not in state.watch_ids:
        return False
    now = float(received_wall or time.time())
    state.last_open_info_at[gid] = now
    note_open_info_received(gid, received_wall=now)
    can_claim = _should_claim_round_events(state)
    issue = str(data.get("issue") or "?")
    # KQ trước next_info — phiên mới không được gỡ C/issue cũ trước khi in kết quả.
    did_result = False
    if can_claim and state.last_open.get(gid) != issue:
        state.last_open[gid] = issue
        run_handlers = True
        if state.broadcast is not None:
            run_handlers = state.broadcast.claim_round_result(
                gid, issue, reporter=state.ws_account
            )
        if run_handlers:
            reporter = state.ws_account or (
                state.broadcast.reporter_of(f"r:{gid}:{issue}")
                if state.broadcast
                else ""
            )
            _emit_round_result_log(gid, data, state)
            _run_handlers_bg(
                f"ws-round-result-{issue}",
                _ROUND_RESULT_HANDLERS,
                gid,
                issue,
                dict(data),
                reporter=reporter,
                log_prefix=state.log_prefix,
            )
            did_result = True
    did_new = False
    nxt = data.get("next_info")
    if can_claim and isinstance(nxt, dict):
        did_new = _print_watch_phiên_mới_from_next(
            gid, nxt, state, received_mono=received_mono
        )
    return did_result or did_new


def _print_watch_game_info(data: dict[str, Any], state: MultiGameWatchState) -> bool:
    """game_info: chỉ log đếm ngược; PHIÊN MỚI lấy từ open_info→next_info."""
    gid = _watch_gid(data)
    if gid not in state.watch_ids:
        return False
    issue = str(data.get("issue") or "")
    open_flag = data.get("is_open")
    if issue.strip():
        if open_flag == 1:
            note_round_bet_open(gid, issue)
        elif open_flag == 0:
            wcd = data.get("wait_countdown_second")
            if wcd is not None:
                try:
                    note_round_bet_open_at(
                        gid,
                        issue,
                        time.monotonic() + max(0.0, float(wcd)),
                        only_if_earlier=True,
                    )
                except (TypeError, ValueError):
                    pass
    cd = data.get("countdown")
    status = "MỞ CƯỢC" if open_flag == 1 else "ĐÓNG CƯỢC"
    if (
        state.log_game_info
        and _should_log_watch_game(gid, state)
        and cd is not None
        and int(cd) in (30, 20, 15, 10, 5, 3, 1)
    ):
        print(
            f"{state.log_prefix} PHIÊN     {_game_tag(gid, state.labels)}  "
            f"issue={issue}  {status}  countdown={cd}s",
            flush=True,
        )
        return True
    return False


def handle_game_watch_message(
    obj: Any,
    state: MultiGameWatchState,
    *,
    debug_ws: bool = False,
    received_mono: float | None = None,
    received_wall: float | None = None,
) -> bool:
    """In sự kiện phiên/jackpot; trả True nếu đã xử lý. Raise ConnectionError nếu logout."""
    if not isinstance(obj, dict):
        return False
    state.msg_count += 1
    t = str(obj.get("type") or "").lower()

    if t == "logout":
        print(f"{state.log_prefix} WS logout: {obj.get('msg')}", flush=True)
        raise ConnectionError(str(obj.get("msg") or "logout"))

    if t == "jackpot_money":
        if not state.full_watch:
            return True
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        return _print_watch_jackpot(data, state)

    if t in ("g_open_info", "open_info"):
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        if debug_ws and _watch_gid(data) in state.watch_ids:
            import json

            print(
                f"DEBUG {t} {_game_tag(_watch_gid(data), state.labels)} "
                f"{json.dumps(data, ensure_ascii=False)[:300]}",
                flush=True,
            )
        return _print_watch_open_info(
            data,
            state,
            received_mono=received_mono,
            received_wall=received_wall,
        )

    if t in ("g_game_info", "game_info"):
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        if debug_ws and _watch_gid(data) in state.watch_ids:
            import json

            print(
                f"DEBUG {t} {_game_tag(_watch_gid(data), state.labels)} "
                f"{json.dumps(data, ensure_ascii=False)[:300]}",
                flush=True,
            )
        return _print_watch_game_info(data, state)

    if t in ("balance", "g_balance"):
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        from xoso66_ws_balance import on_ws_balance_message, parse_ws_balance

        bal = parse_ws_balance(data)
        if bal is not None and state.ws_account:
            on_ws_balance_message(state.ws_account, bal)
        return True

    if not state.full_watch:
        return False
    parsed = _parse_jackpot_money_message(obj)
    if parsed and _print_watch_jackpot(
        {"game_id": parsed.get("game_id"), "money": parsed.get("jackpot")},
        state,
    ):
        return True
    return False


WS_LOGOUT_FULL_REFRESH_COOLDOWN_SEC = float(
    os.environ.get("XOSO66_WS_FULL_REFRESH_COOLDOWN", "90")
)

_WS_TRANSPORT_ERR_HINTS = (
    "no close frame",
    "connection closed",
    "connection reset",
    "broken pipe",
    "eof",
    "timed out",
    "incomplete read",
    "not a socket",
    "10038",
    "10054",
    "10053",
    "winerror",
    "opening handshake",
)


def _is_transport_ws_error(err_s: str) -> bool:
    s = (err_s or "").lower()
    return any(h in s for h in _WS_TRANSPORT_ERR_HINTS)


def _clear_ws_token_cache(session: dict) -> None:
    """Xóa ws_token cache — bắt buộc lấy lại sau verification failed / subscribe lỗi."""
    mg = get_minigame(session)
    for key in ("ws_token", "ws_token_issued_at", "ws_url"):
        mg.pop(key, None)


def _reset_watch_round_dedup(watch: MultiGameWatchState | None) -> None:
    """Socket mới phải nhận lại snapshot open_info — không giữ last_issue của socket cũ."""
    if watch is None:
        return
    watch.last_issue.clear()
    watch.last_open.clear()
    watch.last_open_info_at.clear()


def _cached_ws_token_if_ok(session: dict) -> str | None:
    """Lấy ws_token trong session nếu có — không check age / không ping."""
    mg = get_minigame(session)
    tok = mg.get("ws_token")
    if not tok:
        return None
    return str(tok).strip() or None

# Tránh vipList + nhận thưởng spam mỗi lần WS reconnect (cùng nick).
_WS_AFTER_CONNECT_VIP_LAST_TS: dict[str, float] = {}


def _maybe_sync_withdraw_before_ws(session: dict, account_id: str, username: str) -> None:
    """HTTP paymentorderlist rút → DB + device (trước prep token / mở WS)."""
    try:
        from xoso66_withdraw_tracking import maybe_sync_withdraw_history_on_ws_open

        rep = maybe_sync_withdraw_history_on_ws_open(session, account_id)
        if rep and not rep.get("ok"):
            err = str(rep.get("error") or "?")
            print(f"[WS-WD] [{username}] bỏ qua sync rút: {err}", flush=True)
    except Exception as e:
        print(f"[WS-WD] [{username}] sync rút lỗi: {e}", flush=True)


async def _run_vip_check_after_ws_if_configured(
    account_id: str,
    username: str,
    *,
    game_watch: bool,
) -> None:
    """Sau WS OK + subscribe: check VIP + nhận thưởng (task nền)."""
    if not game_watch:
        return
    env_off = os.environ.get("XOSO66_WS_VIP_AFTER_CONNECT", "1").strip().lower()
    if env_off in ("0", "false", "no", "off"):
        return
    from xoso66_config_util import load_config

    cfg = load_config()
    gw = cfg.get("game_worker") if isinstance(cfg.get("game_worker"), dict) else {}
    if not gw.get("ws_vip_after_connect_enabled", True):
        return
    cooldown = max(0, int(gw.get("ws_vip_after_connect_cooldown_sec") or 3600))
    do_claim = bool(gw.get("ws_vip_after_connect_claim", True))
    aid = str(account_id or "").strip()
    if not aid:
        return

    now = time.time()
    if cooldown > 0:
        last = _WS_AFTER_CONNECT_VIP_LAST_TS.get(aid, 0.0)
        if now - last < cooldown:
            return
        _WS_AFTER_CONNECT_VIP_LAST_TS[aid] = now

    try:
        from xoso66_vip_check import vip_after_ws_connect

        r = await asyncio.to_thread(
            lambda: vip_after_ws_connect(aid, username, do_claim=do_claim)
        )
    except Exception as e:
        print(f"[VIP-WS] [{username}] {e}", flush=True)
        return

    ck = int(r.get("claims_ok") or 0)
    if ck > 0:
        print(f"[VIP-WS] [{username}] đã nhận {ck} thưởng VIP", flush=True)
        if gw.get("ws_vip_after_claim_refresh_balance", True):
            try:
                from xoso66_session import refresh_account_balance_to_db

                await asyncio.to_thread(
                    lambda: refresh_account_balance_to_db(aid, None, refresh=True)
                )
            except Exception as e:
                print(f"[VIP-WS] [{username}] refresh balance lỗi: {e}", flush=True)
    elif not r.get("ok"):
        err = str(r.get("error") or r.get("msg") or "?")
        print(f"[VIP-WS] [{username}] VIP lỗi: {err}", flush=True)


def _schedule_vip_after_ws_connect(
    account_id: str,
    username: str,
    *,
    game_watch: bool,
) -> None:
    """Tạo task nền — vòng recv chạy ngay."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    def _log_vip_task(task: asyncio.Task) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[VIP-WS] Task {task.get_name() or '?'} lỗi: {e}", flush=True)

    t = loop.create_task(
        _run_vip_check_after_ws_if_configured(
            account_id,
            username,
            game_watch=game_watch,
        ),
        name=f"vip-ws-{account_id}",
    )
    t.add_done_callback(_log_vip_task)


def _ws_logout_needs_full_refresh(msg: str | None) -> bool:
    """Server logout — cần refresh user-token + CF, không chỉ ws_token."""
    m = (msg or "").strip().lower()
    if not m:
        return False
    hints = (
        "verification",
        "verify",
        "auth",
        "token",
        "invalid",
        "expired",
        "unauthorized",
        "forbidden",
        "login",
        "session",
    )
    return any(h in m for h in hints)


def _ws_reconnect_flags_after_logout(msg: str | None) -> tuple[bool, bool]:
    """(need_minigame_refresh, need_ws_refresh)."""
    if _ws_logout_needs_full_refresh(msg):
        return True, False
    return False, True


def _flags_after_subscribe_fail(err: str | None) -> tuple[bool, bool]:
    """
    Subscribe fail: (need_minigame_refresh, need_ws_refresh).
    ConnectionClosed / no close frame = ws_token cũ — chỉ getToken.
    verification/logout = refresh full user-token.
    """
    return _ws_reconnect_flags_after_logout(err)


async def _full_refresh_minigame_after_ws_logout(
    session: dict,
    account_id: str,
    *,
    username: str,
    game_key: str,
    last_refresh_at: list[float],
    force: bool = False,
) -> bool:
    """Refresh đầy đủ mini-game sau WS logout; có cooldown tránh Playwright liên tục."""
    from xoso66_minigame_refresh import refresh_minigame_tokens
    from xoso66_session import persist_session

    _clear_ws_token_cache(session)
    now = time.time()
    if (
        not force
        and last_refresh_at[0]
        and (now - last_refresh_at[0]) < WS_LOGOUT_FULL_REFRESH_COOLDOWN_SEC
    ):
        wait = WS_LOGOUT_FULL_REFRESH_COOLDOWN_SEC - (now - last_refresh_at[0])
        print(
            f"[{username}] WS logout — bỏ qua refresh full (cooldown còn {wait:.0f}s)",
            flush=True,
        )
        return False

    print(
        f"[{username}] WS logout — refresh full mini-game (user-token + ws + CF)…",
        flush=True,
    )
    last_refresh_at[0] = now
    rep = await asyncio.to_thread(
        refresh_minigame_tokens,
        session,
        account_id=account_id,
        game_key=game_key,
        force=True,
    )
    mg = rep.get("minigame") or {}
    if rep.get("ok") and mg.get("has_ws_token"):
        persist_session(account_id, session)
        print(f"[{username}] Refresh full mini-game OK", flush=True)
        return True

    err = rep.get("error") or (rep.get("ws_token") or {}).get("msg") or rep
    print(f"[{username}] Refresh full mini-game thất bại: {err}", flush=True)
    return False


def _cloudflare_ipv4_from_ipv6(address: str) -> str | None:
    """Cloudflare AAAA 2606:4700::6812:dd6 → edge IPv4 104.18.13.214."""
    try:
        ip6 = ipaddress.IPv6Address(address)
    except ipaddress.AddressValueError:
        return None
    if ip6 not in ipaddress.IPv6Network("2606:4700::/96"):
        return None
    ip4 = ipaddress.IPv4Address(int(ip6) & 0xFFFFFFFF)
    return str(ip4) if ip4.is_global else None


def _choose_ws_tcp_host(addresses: list[str]) -> str:
    """
    SOCKS5 remote DNS chỉ lấy A record. Nếu A bị trỏ sang domain parking nhưng
    AAAA vẫn là Cloudflare, dùng IPv4 edge nhúng trong AAAA; URI/SNI vẫn WS_HOST.
    """
    forced = (os.environ.get("XOSO66_WS_CONNECT_HOST") or "").strip()
    # Không cho biến môi trường cũ ép ngược về hostname/A parking đang hỏng.
    if (
        forced
        and forced.lower() != WS_HOST.lower()
        and forced not in _WS_PARKING_IPV4
    ):
        return forced
    mapped_ipv4 = [
        mapped
        for address in addresses
        if (mapped := _cloudflare_ipv4_from_ipv6(address))
    ]
    # Domain này có A-record parking không ổn định. Kể cả một lần resolve chỉ
    # thấy AAAA hoặc trả kết quả thiếu, tuyệt đối không đưa hostname cho SOCKS
    # remote-DNS vì nó sẽ lại lấy A parking.
    if WS_HOST.lower() in _WS_BROKEN_A_RECORD_HOSTS:
        return mapped_ipv4[0] if mapped_ipv4 else _WS_CLOUDFLARE_FALLBACK_IPV4[0]
    ipv4 = {x for x in addresses if ":" not in x}
    if not (ipv4 & _WS_PARKING_IPV4):
        return WS_HOST
    if mapped_ipv4:
        return mapped_ipv4[0]
    # DNS đôi lúc chỉ trả A parking, không trả AAAA. Tuyệt đối không quay lại
    # hostname hỏng; dùng edge đã kiểm chứng để WS mới vẫn mở được.
    return _WS_CLOUDFLARE_FALLBACK_IPV4[0]


def _resolve_ws_tcp_host() -> str:
    global _WS_TCP_HOST_CACHE, _WS_TCP_HOST_CACHE_AT
    now = time.time()
    with _WS_TCP_HOST_CACHE_LOCK:
        if (
            _WS_TCP_HOST_CACHE
            and now - _WS_TCP_HOST_CACHE_AT < _WS_TCP_HOST_CACHE_SEC
        ):
            return _WS_TCP_HOST_CACHE
    addresses: list[str] = []
    try:
        for row in socket.getaddrinfo(WS_HOST, 443, type=socket.SOCK_STREAM):
            address = str(row[4][0] or "").strip()
            if address and address not in addresses:
                addresses.append(address)
    except OSError:
        pass
    target = _choose_ws_tcp_host(addresses)
    # Resolver tạm lỗi/rỗng sau khi cache hết hạn: giữ edge tốt trước đó thay
    # vì để SOCKS remote-DNS phân giải hostname về domain parking.
    with _WS_TCP_HOST_CACHE_LOCK:
        previous = _WS_TCP_HOST_CACHE
    if not addresses and previous and previous.lower() != WS_HOST.lower():
        target = previous
    with _WS_TCP_HOST_CACHE_LOCK:
        changed = target != _WS_TCP_HOST_CACHE
        _WS_TCP_HOST_CACHE = target
        _WS_TCP_HOST_CACHE_AT = now
    if changed and target != WS_HOST:
        print(
            f"[WS-DNS] {WS_HOST} A record đang vào domain parking; "
            f"kết nối Cloudflare edge {target} (SNI giữ nguyên)",
            flush=True,
        )
    return target


def _build_socks(
    proxy_str: str, *, timeout: float = SOCKS_CONNECT_TIMEOUT_SEC
) -> tuple[socks.socksocket, str, int]:
    from xoso66_proxy import parse_proxy

    host, port, user, pwd = parse_proxy(proxy_str)
    sock = socks.socksocket()
    sock.set_proxy(socks.SOCKS5, host, port, True, user, pwd)
    sock.settimeout(max(1.0, float(timeout)))
    return sock, host, port


def _ws_timestamp_ms() -> int:
    return int(time.time() * 1000)


def _ws_unique_code(prefix: str) -> str:
    return f"{prefix}-{_ws_timestamp_ms()}-{random.randint(100, 999)}"


def parse_ws_subscribe_spec(spec: str) -> list[int | list[int]]:
    """
    Parse chuỗi subscribe: "0,9,[17,18,19,2]" → [0, 9, [17,18,19,2]].
    Mỗi phần = một frame subscribe gửi lên server.
    """
    out: list[int | list[int]] = []
    spec = (spec or "").strip()
    if not spec:
        return out
    i = 0
    while i < len(spec):
        if spec[i] == "[":
            end = spec.find("]", i)
            if end < 0:
                break
            inner = spec[i + 1 : end]
            ids = [int(x.strip()) for x in inner.split(",") if x.strip().isdigit()]
            if ids:
                out.append(ids)
            i = end + 1
            if i < len(spec) and spec[i] == ",":
                i += 1
            continue
        j = i
        while j < len(spec) and spec[j] not in ",[":
            j += 1
        token = spec[i:j].strip()
        if token.isdigit():
            out.append(int(token))
        i = j + 1 if j < len(spec) and spec[j] == "," else j
    return out


def build_ws_client_message(
    msg_type: str,
    *,
    game_id: int | list[int] | None = None,
    x_lang: str = "vi",
) -> str:
    """Frame JSON client gửi lên WS (subscribe / unsubscribe / ping)."""
    ts = _ws_timestamp_ms()
    if msg_type == "ping":
        body: dict[str, Any] = {
            "type": "ping",
            "unique_code": _ws_unique_code("ping"),
            "time": ts,
            "x-lang": x_lang,
            "data": game_id if game_id is not None else 0,
        }
    elif msg_type in ("subscribe", "unsubscribe"):
        body = {
            "type": msg_type,
            "time": ts,
            "x-lang": x_lang,
            "data": {
                "game_id": game_id if game_id is not None else 0,
                "unique_code": _ws_unique_code(msg_type),
            },
        }
    else:
        body = {"type": msg_type, "time": ts, "x-lang": x_lang}
    return json.dumps(body, ensure_ascii=False)


def flatten_subscribe_plan(
    plan: list[int | list[int]],
    *,
    extra_ids: frozenset[int] | None = None,
) -> list[int]:
    """Mỗi game_id một frame subscribe (batch [17,18,...] chủ yếu cho hũ)."""
    out: list[int] = []
    for item in plan:
        if isinstance(item, list):
            out.extend(int(x) for x in item)
        else:
            out.append(int(item))
    if extra_ids:
        for gid in sorted(extra_ids):
            if gid not in out:
                out.append(gid)
    seen: set[int] = set()
    unique: list[int] = []
    for gid in out:
        if gid not in seen:
            seen.add(gid)
            unique.append(gid)
    return unique


async def ws_send_subscribes(
    ws,
    subscribe_plan: list[int | list[int]],
    *,
    verbose: bool = True,
    individual: bool = False,
) -> None:
    ids = flatten_subscribe_plan(subscribe_plan) if individual else subscribe_plan
    if individual:
        for gid in ids:
            await ws.send(build_ws_client_message("subscribe", game_id=gid))
            if verbose:
                print(f"[WS] → subscribe game_id={gid}", flush=True)
            await asyncio.sleep(0.12)
        return
    for gid in subscribe_plan:
        msg = build_ws_client_message("subscribe", game_id=gid)
        await ws.send(msg)
        if verbose:
            print(f"[WS] → subscribe game_id={gid}", flush=True)
        await asyncio.sleep(0.12)


async def ws_ping_loop(
    ws,
    ping_game_id: int | list[int],
    *,
    interval_sec: float = WS_PING_INTERVAL_SEC,
    stop: asyncio.Event,
) -> None:
    ids = ping_game_id if isinstance(ping_game_id, list) else [int(ping_game_id)]
    if not ids:
        ids = [0]
    idx = 0
    while not stop.is_set():
        await asyncio.sleep(interval_sec)
        if stop.is_set():
            break
        gid = ids[idx % len(ids)]
        idx += 1
        try:
            await ws.send(build_ws_client_message("ping", game_id=gid))
        except Exception:
            break


async def _abort_ws_session(
    *,
    ws: Any = None,
    sock: Any = None,
    reader_task: asyncio.Task | None = None,
    ping_stop: asyncio.Event | None = None,
    ping_task: asyncio.Task | None = None,
    close_timeout: float = 2.0,
    tag: str = "",
) -> None:
    """
    Đóng TCP/WS trước để recv/ping thoát.
    Không cancel ws.recv() trước (Windows SOCKS → kẹt / WinError 10038).
    Mọi await đều có timeout — idle reconnect không được đứng im.
    """
    if ping_stop is not None:
        ping_stop.set()
    if ping_task is not None and not ping_task.done():
        ping_task.cancel()
    if ws is not None:
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(ws.close(), timeout=close_timeout)
    elif sock is not None:
        fd = -1
        with contextlib.suppress(Exception):
            fd = int(sock.fileno())
        print(
            f"[WS-DIAG] abort sock.close fd={fd} (ws=None) "
            f"tag={tag or '-'}",
            flush=True,
        )
        with contextlib.suppress(Exception):
            sock.close()
    if reader_task is not None and not reader_task.done():
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(reader_task), timeout=close_timeout)
        if not reader_task.done():
            reader_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(reader_task, timeout=1.0)
    if ping_task is not None and not ping_task.done():
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(ping_task, timeout=1.0)


def _decode_message(raw: str | bytes) -> Any:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return {"_binary_hex": raw[:200].hex()}
    text = (raw or "").strip()
    if not text:
        return None
    if text in ("ping", "PING"):
        return {"_app_ping": True}
    if text in ("pong", "PONG"):
        return {"_app_pong": True}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw_text": text[:500]}


def _is_ping_payload(obj: Any) -> bool:
    if obj == "ping":
        return True
    if not isinstance(obj, dict):
        return False
    t = str(obj.get("type") or obj.get("cmd") or obj.get("action") or "").lower()
    if t in ("ping", "heartbeat", "heart"):
        return True
    if obj.get("ping") is not None and not obj.get("pong"):
        return True
    return False


def _pong_reply(obj: Any) -> str | None:
    if obj == "ping":
        return "pong"
    if not isinstance(obj, dict):
        return None
    t = str(obj.get("type") or obj.get("cmd") or "").lower()
    if t in ("ping", "heartbeat", "heart"):
        out = dict(obj)
        out["type"] = "pong"
        if "cmd" in out:
            out["cmd"] = "pong"
        out.pop("ping", None)
        out["pong"] = out.get("time") or int(time.time())
        return json.dumps(out, ensure_ascii=False)
    if "ping" in obj:
        return json.dumps({"pong": obj.get("ping"), "time": int(time.time())}, ensure_ascii=False)
    return json.dumps({"type": "pong", "time": int(time.time())}, ensure_ascii=False)


def _parse_jackpot_money_message(obj: dict[str, Any]) -> dict[str, Any] | None:
    """WS type jackpot_money — data.game_id + data.money."""
    if str(obj.get("type") or "").lower() != "jackpot_money":
        return None
    data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
    gid = data.get("game_id")
    money = data.get("money")
    if gid is None and money is None:
        return None
    return {
        "game_id": gid,
        "jackpot": money,
        "group_id": data.get("group_id"),
        "source": "jackpot_money",
    }


def _normalize_jackpot_entry(item: dict[str, Any]) -> dict[str, Any] | None:
    parsed = _parse_jackpot_money_message(item)
    if parsed:
        return parsed
    gid = item.get("game_id") or item.get("gameId") or item.get("gid") or item.get("id")
    amount = None
    for k in _JACKPOT_FIELD_HINTS:
        if k in item and item[k] is not None:
            amount = item[k]
            break
    if amount is None and isinstance(item.get("data"), dict):
        data = item["data"]
        gid = gid or data.get("game_id") or data.get("gameId")
        for k in _JACKPOT_FIELD_HINTS:
            if k in data:
                amount = data[k]
                break
    name = item.get("game_name") or item.get("gameName") or item.get("name")
    if gid is None and amount is None:
        return None
    return {
        "game_id": gid,
        "game_name": name,
        "jackpot": amount,
        "raw_keys": [k for k in item.keys() if k in _JACKPOT_FIELD_HINTS or k in ("game_id", "gameId")],
    }


def _walk_extract_jackpots(obj: Any, found: list[dict[str, Any]]) -> None:
    if isinstance(obj, dict):
        t = str(obj.get("type") or obj.get("cmd") or obj.get("action") or "").lower()
        if t in _JACKPOT_TYPE_HINTS or any(k in obj for k in _JACKPOT_FIELD_HINTS):
            norm = _normalize_jackpot_entry(obj)
            if norm:
                found.append(norm)
            elif any(k in obj for k in _JACKPOT_FIELD_HINTS):
                found.append({"game_id": obj.get("game_id"), "jackpot": obj, "raw": True})

        for key in ("jackpots", "jackpot_list", "games", "list", "data", "items"):
            child = obj.get(key)
            if isinstance(child, list):
                for it in child:
                    if isinstance(it, dict):
                        n = _normalize_jackpot_entry(it)
                        if n:
                            found.append(n)
                        else:
                            _walk_extract_jackpots(it, found)
            elif isinstance(child, dict):
                _walk_extract_jackpots(child, found)

        for v in obj.values():
            if isinstance(v, (dict, list)) and v is not obj.get("data"):
                _walk_extract_jackpots(v, found)
    elif isinstance(obj, list):
        for it in obj:
            _walk_extract_jackpots(it, found)


def apply_jackpot_updates(state: JackpotState, found: list[dict[str, Any]]) -> bool:
    if not found:
        return False
    changed = False
    for entry in found:
        gid = str(entry.get("game_id") or "unknown")
        prev = state.by_game.get(gid)
        if prev != entry:
            state.by_game[gid] = entry
            changed = True
        state.last_raw.append(entry)
        if len(state.last_raw) > 50:
            state.last_raw.pop(0)
    if changed:
        state.jackpot_updates += 1
    return changed


def format_jackpot_table(state: JackpotState) -> str:
    if not state.by_game:
        return "(no jackpot yet)"
    lines = ["game_id | jackpot", "--------+--------"]
    for gid in sorted(state.by_game.keys(), key=lambda x: (x == "unknown", x)):
        row = state.by_game[gid]
        jp = row.get("jackpot")
        name = row.get("game_name") or ""
        extra = f" ({name})" if name else ""
        lines.append(f"{gid:7} | {jp}{extra}")
    return "\n".join(lines)


_ws_connect_states: dict[int, dict[str, Any]] = {}
_ws_connect_states_lock = threading.Lock()


def reset_ws_connect_limit() -> None:
    """Soft-restart — bỏ gate của mọi event loop cũ."""
    with _ws_connect_states_lock:
        _ws_connect_states.clear()


async def _ensure_ws_connect_sem() -> asyncio.Semaphore:
    """Một Semaphore riêng cho từng loop (listener loop tách pool loop)."""
    from xoso66_config_util import load_config
    from xoso66_ws_pool import ws_connect_batch_size

    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    with _ws_connect_states_lock:
        state = _ws_connect_states.get(loop_id)
        if state is None:
            state = {
                "lock": asyncio.Lock(),
                "sem": None,
                "limit": 0,
            }
            _ws_connect_states[loop_id] = state
    async with state["lock"]:
        limit = ws_connect_batch_size(load_config())
        if state["sem"] is None or int(state["limit"]) != limit:
            state["sem"] = asyncio.Semaphore(limit)
            state["limit"] = limit
        return state["sem"]


class _WsConnectSlot:
    """Giữ gate lúc SOCKS + TLS handshake (nhả trước subscribe)."""

    def __init__(self, timeout: float | None = None) -> None:
        self._sem: asyncio.Semaphore | None = None
        self._timeout = timeout

    async def __aenter__(self) -> "_WsConnectSlot":
        try:
            sem = await _ensure_ws_connect_sem()
            await self._acquire(sem)
        except RuntimeError as e:
            msg = str(e).lower()
            if "different event loop" in msg or "is bound to" in msg:
                reset_ws_connect_limit()
                sem = await _ensure_ws_connect_sem()
                await self._acquire(sem)
            else:
                raise
        self._sem = sem
        return self

    async def _acquire(self, sem: asyncio.Semaphore) -> None:
        wait = self._timeout
        if wait is not None and wait > 0:
            await asyncio.wait_for(sem.acquire(), timeout=wait)
        else:
            await sem.acquire()

    async def __aexit__(self, *exc: object) -> None:
        if self._sem is not None:
            self._sem.release()
            self._sem = None


def ws_connect_slot(timeout: float | None = None) -> _WsConnectSlot:
    return _WsConnectSlot(timeout=timeout)


def ws_reconnect_interval_sec(*, game_watch: bool = True) -> float:
    return WS_CONNECT_BUDGET_SEC if game_watch else 5.0


async def _sleep_until_reconnect(
    started_at: float,
    *,
    interval_sec: float,
    is_stopping,
    min_sleep_sec: float = 2.0,
    jitter_sec: float = 1.5,
) -> None:
    """Ngủ đến mốc started+interval; tối thiểu min_sleep+jitter (không retry 0s)."""
    extra = random.uniform(0.0, max(0.0, float(jitter_sec))) if jitter_sec else 0.0
    until = max(
        float(started_at) + float(interval_sec),
        time.time() + max(0.0, float(min_sleep_sec)) + extra,
    )
    while not is_stopping():
        remain = until - time.time()
        if remain <= 0:
            return
        await asyncio.sleep(min(1.0, remain))


async def _with_ws_connect_limit(coro):
    """Giới hạn connect đồng thời — giữ API cũ (chỉ wrap 1 coro)."""
    async with ws_connect_slot():
        return await coro


async def _socks_tcp_connect(
    proxy_str: str, *, timeout: float = SOCKS_CONNECT_TIMEOUT_SEC
):
    """SOCKS TCP tới WS host — trong gate, có timeout (không treo)."""
    sock, _ph, _pp = _build_socks(proxy_str, timeout=timeout)
    connect_host = await asyncio.to_thread(_resolve_ws_tcp_host)
    try:
        await asyncio.to_thread(sock.connect, (connect_host, 443))
    except Exception as e:
        with contextlib.suppress(Exception):
            sock.close()
        raise ConnectionError(
            f"SOCKS connect {connect_host}:443 "
            f"(WS host {WS_HOST}) failed: {e}"
        ) from e
    except BaseException:
        with contextlib.suppress(Exception):
            sock.close()
        raise
    with contextlib.suppress(Exception):
        sock.settimeout(None)
    return sock


async def _ws_handshake(
    ws_url: str,
    sock,
    *,
    origin: str = MINIGAME_BASE,
    open_timeout: float = 10.0,
):
    """TLS/WSS handshake trên sock đã connect — đoạn nhạy Windows, nằm trong gate."""
    import websockets

    # websockets.connect(sock=...) nhận ownership ngay khi gọi — handshake fail
    # mà sock.close() lại → WinError 10038 lan Proactor/event loop.
    try:
        ws = await websockets.connect(
            ws_url,
            sock=sock,
            ssl=True,
            ping_interval=None,
            ping_timeout=None,
            open_timeout=max(1.0, float(open_timeout)),
            close_timeout=5,
            origin=origin,
            user_agent_header=os.environ.get("XOSO66_WS_UA", DEFAULT_UA),
            max_size=2**22,
        )
    except Exception:
        # Không sock.close() — sock đã/ có thể thuộc websockets (tránh 10038).
        raise
    return ws, None


async def _connect_ws(
    ws_url: str,
    proxy_str: str,
    *,
    origin: str = MINIGAME_BASE,
    ping_interval: float = 25.0,
    ping_timeout: float = 20.0,
):
    """Tương thích cũ: SOCKS + handshake trong gate."""
    _ = ping_interval, ping_timeout
    async with ws_connect_slot(timeout=WS_CONNECT_SLOT_WAIT_SEC):
        sock = await _socks_tcp_connect(
            proxy_str, timeout=SOCKS_CONNECT_TIMEOUT_SEC
        )
        return await _ws_handshake(
            ws_url, sock, origin=origin, open_timeout=10.0
        )


async def listen_minigame_ws(
    session: dict,
    account_id: str,
    *,
    duration_sec: float = 0,
    game_key: str = "taixiu_dai_loc",
    refresh_before_connect: bool = True,
    ws_token_override: str | None = None,
    verbose: bool = True,
    game_watch: bool = True,
    watch_rounds: bool | None = None,
    focus_backup_game_id: int | None = None,
    focus_game_id_provider: Callable[[], int] | None = None,
    watch_game_ids: frozenset[int] | None = None,
    subscribe_spec: str | None = None,
    ping_game_id: int | None = None,
    debug_ws: bool = False,
    subscribe_individual: bool = False,
    save_jackpot: bool = True,
    jackpot_store: Any = None,
    log_game_info: bool = False,
    broadcast_coordinator: WsBroadcastCoordinator | None = None,
    conn_gen: str | None = None,
) -> JackpotState:
    """
    Kết nối WS, giữ ping, in jackpot + phiên/kết quả. duration_sec=0 → chạy đến Ctrl+C.
    """
    from xoso66_minigame_session import get_ws_token
    from xoso66_minigame_catalog import game_by_key
    from xoso66_proxy import ensure_proxy
    from xoso66_session import ensure_session

    g = game_by_key(game_key)
    primary_gid = int(g["game_id"])
    subscribe_plan = parse_ws_subscribe_spec(subscribe_spec or DEFAULT_WS_SUBSCRIBE)
    if not subscribe_plan:
        subscribe_plan = [0, primary_gid]
    watch_ids = watch_game_ids if watch_game_ids is not None else frozenset(DEFAULT_WATCH_GAME_IDS)
    full_watch = bool(watch_rounds) if watch_rounds is not None else bool(game_watch)
    backup_gid = int(focus_backup_game_id or 0)
    current_backup_gid = backup_gid
    if not full_watch and backup_gid > 0:
        watch_ids = frozenset({backup_gid})
    do_watch = full_watch or backup_gid > 0
    multi_watch = bool(do_watch and len(watch_ids) > 1)
    solo_watch_gid = next(iter(watch_ids)) if do_watch and len(watch_ids) == 1 else None
    ping_gid: int | list[int]
    if ping_game_id is not None:
        if isinstance(ping_game_id, (list, tuple, frozenset, set)):
            ping_gid = [int(x) for x in ping_game_id]
        else:
            ping_gid = int(ping_game_id)
    elif solo_watch_gid is not None:
        ping_gid = int(solo_watch_gid)
    elif multi_watch:
        ping_gid = sorted(watch_ids)
    else:
        ping_gid = primary_gid

    from xoso66_accounts_db import username_for_log

    aid = account_id or str(session.get("id") or "")
    # Chỉ load session + proxy — không check balance, không ping/prep token.
    # Có ws_token cache thì dùng; thiếu thì get_ws_token trong vòng connect (lỗi → retry sau).
    session = await asyncio.to_thread(ensure_session, aid, force_login=False)
    await asyncio.to_thread(ensure_proxy, session)

    username = username_for_log(aid, session)

    if game_watch:
        from xoso66_round_log import log_ws_connecting

        log_ws_connecting(username, user_token_ok=True)

    proxy_str = session["proxy"]

    jp_store = None
    if save_jackpot:
        if jackpot_store is not None:
            jp_store = jackpot_store
        else:
            from xoso66_minigame_jackpot_store import MinigameJackpotStore

            jp_store = MinigameJackpotStore()

    state = JackpotState()
    log_prefix = f"[{username}] " if game_watch else ""
    watch = (
        MultiGameWatchState(
            watch_ids=watch_ids,
            labels=dict(GAME_ID_LABELS),
            jackpot_store=jp_store,
            ws_account=aid,
            log_prefix=log_prefix,
            log_game_info=log_game_info,
            broadcast=broadcast_coordinator,
            full_watch=full_watch,
        )
        if do_watch
        else None
    )
    if watch is not None and jp_store is not None:
        _refresh_focus_game(watch)
        if watch.focus_game_id is None:
            from xoso66_config_util import load_config, main_progress

            ab = load_config().get("auto_bet")
            min_jp = (
                float(ab.get("min_jackpot_vnd") or 0)
                if isinstance(ab, dict)
                else 0.0
            )
            if min_jp > 0:
                main_progress(
                    f"[{username}] WS OK — lưu hũ 5 game; "
                    f"log phiên khi có game ≥ {min_jp:,.0f}đ"
                )
    deadline = time.time() + duration_sec if duration_sec > 0 else None
    reconnect_interval = ws_reconnect_interval_sec(game_watch=game_watch)
    reconnect_delay = reconnect_interval
    reconnect_base = reconnect_interval
    reconnect_max = reconnect_interval
    reconnect_step = 0.0
    manual_ws_token = bool(ws_token_override)
    need_ws_refresh = False
    need_minigame_refresh = False
    last_full_refresh_at: list[float] = [0.0]
    verify_fail_streak = 0
    had_live_ws = False
    transport_fail_streak = 0
    subscribe_closed_streak = 0
    quick_retry = False
    # Spawn thường: dùng cache. Force chỉ khi verification / refresh_before_connect.
    force_fresh_tokens = bool(refresh_before_connect)

    from xoso66_shutdown import stopping

    while True:
        quick_retry = False
        if stopping():
            break
        if deadline and time.time() >= deadline:
            break

        if not full_watch and focus_game_id_provider is not None:
            with contextlib.suppress(Exception):
                fresh_gid = int(focus_game_id_provider() or 0)
                if fresh_gid > 0:
                    current_backup_gid = fresh_gid
                    subscribe_plan = [0, fresh_gid]
                    if watch is not None:
                        watch.watch_ids = frozenset({fresh_gid})

        if need_minigame_refresh:
            need_minigame_refresh = False
            with contextlib.suppress(Exception):
                from xoso66_minigame_ws_worker import note_ws_task_activity

                note_ws_task_activity(aid)
            refreshed = await _full_refresh_minigame_after_ws_logout(
                session,
                aid,
                username=username,
                game_key=game_key,
                last_refresh_at=last_full_refresh_at,
                force=force_fresh_tokens or verify_fail_streak > 0,
            )
            session = await asyncio.to_thread(ensure_session, aid, force_login=False)
            await asyncio.to_thread(ensure_proxy, session)
            proxy_str = session["proxy"]
            if not refreshed:
                rep = await asyncio.to_thread(
                    refresh_minigame_tokens,
                    session,
                    account_id=aid,
                    game_key=game_key,
                    force=True,
                    ws_only=False,
                )
                session = await asyncio.to_thread(ensure_session, aid, force_login=False)
                refreshed = bool(rep.get("ok") and (rep.get("minigame") or {}).get("has_ws_token"))
                if not refreshed:
                    need_ws_refresh = True

        connect_started_at = time.time()
        slot_wait_s = 0.0
        token_s = 0.0
        io_t0 = 0.0
        with contextlib.suppress(Exception):
            from xoso66_minigame_ws_worker import note_ws_task_activity

            note_ws_task_activity(aid)
        if game_watch:
            print(f"[WS] [{username}] connect…", flush=True)

        ws_url = None
        ws = None
        sock = None
        ping_stop = asyncio.Event()
        ping_task: asyncio.Task | None = None
        got_slot = False
        token: str | None = None
        try:
            # Token ngoài cửa handshake — getToken 45s không chặn nick khác SOCKS/TLS.
            if ws_token_override and manual_ws_token:
                token = ws_token_override.strip()
                mg = get_minigame(session)
                mg["ws_token"] = token
                mg["ws_url"] = ws_url_from_token(token)
            else:
                skip_cache = force_fresh_tokens or need_ws_refresh
                cached = None if skip_cache else _cached_ws_token_if_ok(session)
                if cached:
                    token = cached
                else:
                    if had_live_ws and game_watch:
                        print(
                            f"🔐 [{username}] đang lấy lại ws_token…",
                            flush=True,
                        )
                    t_tok = time.time()
                    token = await asyncio.wait_for(
                        asyncio.to_thread(
                            get_ws_token,
                            session,
                            aid,
                            game_key=game_key,
                            force_refresh=bool(
                                need_ws_refresh or force_fresh_tokens
                            ),
                        ),
                        timeout=WS_TOKEN_BUDGET_SEC,
                    )
                    token_s = time.time() - t_tok
                need_ws_refresh = False
                manual_ws_token = False
                ws_token_override = None
            ws_url = ws_url_from_token(token)
            if game_watch:
                print(
                    f"[WS-DIAG] [{username}] token-ok "
                    f"get={token_s:.1f}s "
                    f"{'cache' if token_s <= 0.0 else 'fetch'} "
                    f"force={force_fresh_tokens}",
                    flush=True,
                )

            token_ready_at = time.time()
            async with ws_connect_slot(timeout=WS_CONNECT_SLOT_WAIT_SEC):
                got_slot = True
                io_t0 = time.time()
                slot_wait_s = io_t0 - token_ready_at
                if game_watch:
                    print(
                        f"[WS-DIAG] [{username}] got-slot "
                        f"wait={slot_wait_s:.1f}s force={force_fresh_tokens}",
                        flush=True,
                    )
                sock = await _socks_tcp_connect(
                    proxy_str,
                    timeout=SOCKS_CONNECT_TIMEOUT_SEC,
                )
                try:
                    ws, sock = await _ws_handshake(
                        ws_url,
                        sock,
                        open_timeout=8.0,
                    )
                except Exception as e:
                    print(
                        f"[WS-DIAG] [{username}] handshake fail "
                        f"slot={got_slot} sock={'yes' if sock else 'no'} "
                        f"slot_wait={slot_wait_s:.1f}s token={token_s:.1f}s "
                        f"{type(e).__name__}: {e}",
                        flush=True,
                    )
                    raise
            ping_task = asyncio.create_task(
                ws_ping_loop(ws, ping_gid, stop=ping_stop)
            )
            subscribe_ok = False
            subscribe_err = ""
            try:
                await asyncio.wait_for(
                    ws_send_subscribes(
                        ws,
                        subscribe_plan,
                        verbose=verbose,
                        individual=subscribe_individual,
                    ),
                    timeout=max(1.0, WS_SUBSCRIBE_TIMEOUT_SEC),
                )
                subscribe_ok = True
                subscribe_closed_streak = 0
            except Exception as e:
                subscribe_err = f"{type(e).__name__}: {e}".strip()
                print(
                    f"[WS] [{username}] subscribe failed: {subscribe_err or '(empty)'} "
                    f"[WS-DIAG] ws={'yes' if ws else 'no'} "
                    f"slot_wait={slot_wait_s:.1f}s token={token_s:.1f}s",
                    flush=True,
                )
                err_l = subscribe_err.lower()
                closed_race = (
                    "no close frame" in err_l or "connectionclosed" in err_l
                )
                if closed_race and subscribe_closed_streak < 1:
                    subscribe_closed_streak += 1
                    quick_retry = True
                    if game_watch:
                        print(
                            f"[WS-DIAG] [{username}] subscribe-fail → "
                            f"giữ token, mở lại ngay",
                            flush=True,
                        )
                else:
                    _clear_ws_token_cache(session)
                    full, ws_only = _flags_after_subscribe_fail(subscribe_err)
                    need_minigame_refresh = bool(full)
                    need_ws_refresh = bool(ws_only or not full)
                    force_fresh_tokens = True
                    if game_watch:
                        path = "full-refresh" if full else "getToken"
                        print(
                            f"[WS-DIAG] [{username}] subscribe-fail → {path}",
                            flush=True,
                        )
            if not subscribe_ok:
                ping_stop.set()
                if ping_task is not None:
                    ping_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await ping_task
                    ping_task = None
                if ws is not None:
                    with contextlib.suppress(Exception):
                        await ws.close()
                    ws = None
                raise ConnectionError(
                    f"subscribe failed: {subscribe_err or 'unknown'}"
                )
            if game_watch:
                from xoso66_round_log import log_ws_connected

                with contextlib.suppress(Exception):
                    from xoso66_ws_pool import register_ws_connected

                    register_ws_connected(aid, conn_gen=conn_gen)
                with contextlib.suppress(Exception):
                    from xoso66_minigame_ws_worker import mark_ws_prune_connected

                    mark_ws_prune_connected(aid)
                first_live = not had_live_ws
                if first_live:
                    log_ws_connected(username, account_id=aid)
                    _schedule_vip_after_ws_connect(
                        aid, username, game_watch=game_watch
                    )
                    # Sync rút sau connect — không chặn handshake hàng loạt.
                    asyncio.create_task(
                        asyncio.to_thread(
                            _maybe_sync_withdraw_before_ws,
                            session,
                            aid,
                            username,
                        ),
                        name=f"ws-wd-{aid}",
                    )
                else:
                    print(
                        f"[WS] [{username}] reconnect OK — subscribe lại",
                        flush=True,
                    )
                had_live_ws = True
                transport_fail_streak = 0
                reconnect_delay = reconnect_base
                force_fresh_tokens = False
                verify_fail_streak = 0
            if verbose and not game_watch:
                print(
                    f"[WS] {username} connected subscribe={subscribe_plan} ping={ping_gid}",
                    flush=True,
                )

            # Reader riêng: critical frame không đứng sau jackpot/game_info backlog.
            inbound = WsInboundRouter(critical_maxsize=64)
            last_msg_at = time.time()
            socket_opened_at = last_msg_at
            idle_reconnect_sec = 120.0
            open_info_stale_sec = max(0.0, float(WS_OPEN_INFO_STALE_SEC or 0))
            stale_reconnect = False
            next_focus_check_mono = time.monotonic() + 2.0

            async def _ws_reader() -> None:
                nonlocal last_msg_at
                try:
                    while True:
                        msg = await ws.recv()
                        item = inbound.put_raw(msg)
                        # Idle health tính lúc raw frame tới, không phải lúc handler chạy.
                        last_msg_at = item.received_wall
                        note_ws_ingress_health(
                            aid,
                            inbound,
                            received_wall=item.received_wall,
                        )
                        if isinstance(item.obj, dict) and str(
                            item.obj.get("type") or ""
                        ).lower() in ("g_open_info", "open_info"):
                            raw_data = (
                                item.obj.get("data")
                                if isinstance(item.obj.get("data"), dict)
                                else {}
                            )
                            note_open_info_received(
                                _watch_gid(raw_data),
                                received_wall=item.received_wall,
                            )
                except Exception:
                    pass
                finally:
                    inbound.close()

            def _open_info_stale_reason() -> str:
                """Chỉ listener full: hết open_info = chết kênh phiên.

                Player subscribe 0+focus — thường không có open_info định kỳ.
                Đừng reconnect 90s rồi tranh claim snapshot.
                """
                if (
                    watch is None
                    or open_info_stale_sec <= 0
                    or not watch.full_watch
                ):
                    return ""
                newest = max(watch.last_open_info_at.values(), default=0.0)
                ref = newest or socket_opened_at
                age = time.time() - ref
                if age > open_info_stale_sec:
                    return f"không open_info {age:.0f}s"
                return ""

            reader_task = asyncio.create_task(
                _ws_reader(), name=f"ws-reader-{aid}"
            )
            try:
                while True:
                    if stopping():
                        break
                    if deadline and time.time() >= deadline:
                        break
                    if (
                        not full_watch
                        and focus_game_id_provider is not None
                        and time.monotonic() >= next_focus_check_mono
                    ):
                        next_focus_check_mono = time.monotonic() + 2.0
                        try:
                            new_gid = int(focus_game_id_provider() or 0)
                        except Exception:
                            new_gid = 0
                        if new_gid > 0 and new_gid != current_backup_gid:
                            # Không tạo gap: subscribe game mới trước, rồi bỏ game cũ.
                            await ws.send(
                                build_ws_client_message(
                                    "subscribe", game_id=new_gid
                                )
                            )
                            if current_backup_gid > 0:
                                await ws.send(
                                    build_ws_client_message(
                                        "unsubscribe",
                                        game_id=current_backup_gid,
                                    )
                                )
                            current_backup_gid = new_gid
                            if watch is not None:
                                watch.watch_ids = frozenset({new_gid})
                    stale_why = _open_info_stale_reason()
                    if stale_why:
                        if verbose or game_watch:
                            print(
                                f"[WS] [{username}] {stale_why} "
                                f"— reconnect (token mới)",
                                flush=True,
                            )
                        stale_reconnect = True
                        break
                    if time.time() - last_msg_at > idle_reconnect_sec:
                        if verbose or game_watch:
                            print(
                                f"[WS] [{username}] idle {idle_reconnect_sec:.0f}s "
                                f"— reconnect (token mới)",
                                flush=True,
                            )
                        stale_reconnect = True
                        break
                    try:
                        item = await asyncio.wait_for(inbound.get(), timeout=0.2)
                    except asyncio.TimeoutError:
                        continue
                    if item is None:
                        break

                    note_ws_ingress_health(
                        aid,
                        inbound,
                        queue_age_sec=max(
                            0.0, time.monotonic() - item.received_mono
                        ),
                    )
                    state.msg_count += 1
                    obj = item.obj

                    if watch is not None and handle_game_watch_message(
                        obj,
                        watch,
                        debug_ws=debug_ws,
                        received_mono=item.received_mono,
                        received_wall=item.received_wall,
                    ):
                        continue

                    if isinstance(obj, dict) and str(obj.get("type") or "").lower() == "logout":
                        logout_msg = str(obj.get("msg") or "")
                        full, ws_only = _ws_reconnect_flags_after_logout(logout_msg)
                        action = (
                            "refresh full mini-game"
                            if full
                            else "refresh ws token"
                        )
                        print(
                            f"[WS] logout: {logout_msg or '?'} — {action}",
                            flush=True,
                        )
                        if "verification" in logout_msg.lower():
                            verify_fail_streak += 1
                            force_fresh_tokens = True
                            _clear_ws_token_cache(session)
                            reconnect_delay = min(
                                45.0,
                                reconnect_delay * (1.0 + 0.5 * verify_fail_streak),
                            )
                        else:
                            verify_fail_streak = 0
                        need_minigame_refresh = need_minigame_refresh or full
                        need_ws_refresh = need_ws_refresh or ws_only
                        if "verification" in logout_msg.lower():
                            need_minigame_refresh = True
                            need_ws_refresh = False
                        manual_ws_token = False
                        ws_token_override = None
                        break

                    if watch is not None:
                        continue

                    # Pool keep-alive: ping/logout only — không parse hũ/phiên.
                    if watch_rounds is False:
                        if isinstance(obj, dict) and obj.get("_app_ping"):
                            await ws.send("pong")
                            continue
                        if _is_ping_payload(obj):
                            reply = _pong_reply(obj)
                            if reply:
                                await ws.send(reply)
                        continue

                    if isinstance(obj, dict) and str(obj.get("type") or "").lower() in (
                        "jackpot",
                        "jackpots",
                        "jackpot_update",
                        "jackpot_money",
                        "pool",
                        "pool_update",
                    ):
                        found: list[dict[str, Any]] = []
                        _walk_extract_jackpots(obj, found)
                        if not found:
                            found.append(
                                {
                                    "game_id": obj.get("game_id"),
                                    "jackpot": obj.get("data") or obj,
                                    "raw": True,
                                }
                            )
                        if apply_jackpot_updates(state, found):
                            print(
                                f"\n[JACKPOT] #{state.jackpot_updates} @ {time.strftime('%H:%M:%S')}\n"
                                f"{format_jackpot_table(state)}\n",
                                flush=True,
                            )
                        continue

                    if isinstance(obj, dict) and obj.get("_app_ping"):
                        await ws.send("pong")
                        continue

                    if _is_ping_payload(obj):
                        reply = _pong_reply(obj)
                        if reply:
                            await ws.send(reply)
                        continue

                    found: list[dict[str, Any]] = []
                    _walk_extract_jackpots(obj, found)
                    if apply_jackpot_updates(state, found):
                        print(
                            f"\n[JACKPOT] #{state.jackpot_updates} @ {time.strftime('%H:%M:%S')}\n"
                            f"{format_jackpot_table(state)}\n",
                            flush=True,
                        )
                    elif verbose and state.msg_count <= 15:
                        preview = raw if isinstance(raw, str) else str(raw)[:300]
                        print(f"[WS] msg#{state.msg_count}: {preview[:280]}", flush=True)
                    elif verbose and state.msg_count == 16:
                        print("[WS] (suppress raw log — jackpot only)", flush=True)
            finally:
                # Đóng socket TRƯỚC — cancel recv() trên SOCKS chết sẽ kẹt mãi
                # (idle log xong không reconnect, health connect=1 / open_info STALE).
                await _abort_ws_session(
                    ws=ws,
                    sock=sock,
                    reader_task=reader_task,
                    ping_stop=ping_stop,
                    ping_task=ping_task,
                    tag=username,
                )
                ws = None
                sock = None
                ping_task = None
            if stale_reconnect:
                # Socket còn ping/hũ nhưng kênh phiên chết, hoặc idle 120s:
                # cache ws_token cũ không subscribe lại được.
                need_ws_refresh = True
                force_fresh_tokens = True
                _clear_ws_token_cache(session)
                _reset_watch_round_dedup(watch)
                reconnect_delay = min(float(reconnect_delay or 3.0), 3.0)
                with contextlib.suppress(Exception):
                    from xoso66_ws_pool import unregister_ws_connected

                    unregister_ws_connected(aid, conn_gen=conn_gen)

        except asyncio.CancelledError:
            print(
                f"[WS-DIAG] [{username}] cancelled "
                f"slot={got_slot} token={'yes' if token else 'no'} "
                f"ws={'yes' if ws else 'no'} "
                f"slot_wait={slot_wait_s:.1f}s token_get={token_s:.1f}s",
                flush=True,
            )
            raise
        except TimeoutError as e:
            io_elapsed = (time.time() - io_t0) if io_t0 else 0.0
            if token is None:
                need_ws_refresh = True
                tok_err = str(e).strip() or "getToken quá hạn"
                print(
                    f"[WS] [{username}] ws_token failed: {tok_err} "
                    f"[WS-DIAG] slot_wait={slot_wait_s:.1f}s "
                    f"io={io_elapsed:.1f}s force={force_fresh_tokens} "
                    f"budget={WS_TOKEN_BUDGET_SEC:.0f}s",
                    flush=True,
                )
            elif not got_slot:
                print(
                    f"[WS] [{username}] chờ cửa mở WS quá "
                    f"{WS_CONNECT_SLOT_WAIT_SEC:.0f}s — thử lại "
                    f"[WS-DIAG] queued={time.time() - connect_started_at:.1f}s",
                    flush=True,
                )
            else:
                msg = str(e).strip() or "handshake/subscribe quá hạn"
                print(
                    f"[WS] [{username}] mất kết nối: {msg} — reconnect sau "
                    f"{reconnect_interval}s "
                    f"[WS-DIAG] slot_wait={slot_wait_s:.1f}s "
                    f"token={token_s:.1f}s io={io_elapsed:.1f}s",
                    flush=True,
                )
        except (ConnectionResetError, OSError) as e:
            # WinError 10038/995: nuốt tại nick — không lan pool (LC79).
            winerr = getattr(e, "winerror", None)
            if winerr in (995, 10038):
                reconnect_delay = min(
                    reconnect_max, reconnect_delay + reconnect_step
                )
                if reconnect_delay < reconnect_base:
                    reconnect_delay = reconnect_base
                if game_watch:
                    print(
                        f"[WS] [{username}] socket {winerr} — chỉ nick này, "
                        f"reconnect {reconnect_delay:.0f}s "
                        f"[WS-DIAG] slot={got_slot} token={'yes' if token else 'no'} "
                        f"ws={'yes' if ws else 'no'} {e}",
                        flush=True,
                    )
            else:
                err_s = str(e)
                if "socks connect" in err_s.lower():
                    from xoso66_proxy import maybe_report_proxy_dead_from_exception

                    maybe_report_proxy_dead_from_exception(
                        aid,
                        e,
                        proxy_str=proxy_str,
                        source="WS connect",
                    )
                full, ws_only = _ws_reconnect_flags_after_logout(err_s)
                if "verification" in err_s.lower():
                    verify_fail_streak += 1
                    force_fresh_tokens = True
                    _clear_ws_token_cache(session)
                    reconnect_delay = min(
                        45.0,
                        reconnect_delay * (1.0 + 0.5 * verify_fail_streak),
                    )
                else:
                    verify_fail_streak = 0
                need_minigame_refresh = need_minigame_refresh or full
                need_ws_refresh = need_ws_refresh or ws_only
                if "verification" in err_s.lower():
                    need_minigame_refresh = True
                    need_ws_refresh = False
                err_show = str(e).strip() or type(e).__name__
                print(
                    f"[WS] [{username}] mất kết nối: {err_show} — reconnect sau {reconnect_interval}s",
                    flush=True,
                )
        except ConnectionError as e:
            err_s = str(e)
            if "socks connect" in err_s.lower():
                from xoso66_proxy import maybe_report_proxy_dead_from_exception

                maybe_report_proxy_dead_from_exception(
                    aid,
                    e,
                    proxy_str=proxy_str,
                    source="WS connect",
                )
            full, ws_only = _ws_reconnect_flags_after_logout(err_s)
            if "verification" in err_s.lower():
                verify_fail_streak += 1
                force_fresh_tokens = True
                _clear_ws_token_cache(session)
                reconnect_delay = min(
                    45.0,
                    reconnect_delay * (1.0 + 0.5 * verify_fail_streak),
                )
            else:
                verify_fail_streak = 0
            need_minigame_refresh = need_minigame_refresh or full
            need_ws_refresh = need_ws_refresh or ws_only
            if "verification" in err_s.lower():
                need_minigame_refresh = True
                need_ws_refresh = False
            err_show = str(e).strip() or type(e).__name__
            wait_s = 2.0 if quick_retry else reconnect_interval
            print(
                f"[WS] [{username}] mất kết nối: {err_show} — reconnect sau {wait_s:.0f}s",
                flush=True,
            )
            # Không break outer while — fallthrough finally → sleep.
        except Exception as e:
            if token is None:
                err_tok = str(e).lower()
                if "user-token" in err_tok or "user_token" in err_tok:
                    need_minigame_refresh = True
                    force_fresh_tokens = True
                need_ws_refresh = True
                from xoso66_proxy import maybe_report_proxy_dead_from_exception

                maybe_report_proxy_dead_from_exception(
                    aid,
                    e,
                    proxy_str=proxy_str,
                    source="ws_token",
                )
                tok_err = str(e).strip() or type(e).__name__
                print(
                    f"[WS] [{username}] ws_token failed: {tok_err}",
                    flush=True,
                )
            elif _is_transport_ws_error(str(e).lower()):
                from xoso66_proxy import maybe_report_proxy_dead_from_exception

                # Handshake timeout ≠ proxy chết (maybe_* tự bỏ qua).
                maybe_report_proxy_dead_from_exception(
                    aid,
                    e,
                    proxy_str=proxy_str,
                    source="WS transport",
                )
                transport_fail_streak += 1
                # LC79-style: backoff 20→60s — không reconnect 2–5s (stampede gate).
                reconnect_delay = min(
                    reconnect_max,
                    max(reconnect_base, reconnect_delay + reconnect_step),
                )
                if transport_fail_streak >= 5:
                    reconnect_delay = reconnect_max
                print(
                    f"[WS] [{username}] Error: {e} — reconnect in {reconnect_interval:.0f}s",
                    flush=True,
                )
            else:
                err_s = str(e).lower()
                if _ws_logout_needs_full_refresh(err_s):
                    need_minigame_refresh = True
                elif "user-token" in err_s or "user_token" in err_s:
                    need_minigame_refresh = True
                    force_fresh_tokens = True
                    need_ws_refresh = True
                elif "ws_token" in err_s:
                    need_ws_refresh = True
                print(
                    f"[WS] [{username}] Error: {e} — reconnect in {reconnect_interval}s",
                    flush=True,
                )
            # Không break outer while — fallthrough finally → sleep → mở lại.
        finally:
            if game_watch:
                # Chỉ bỏ khỏi «đã connect» — task còn vòng reconnect in-task (LC79-style).
                with contextlib.suppress(Exception):
                    from xoso66_ws_pool import unregister_ws_connected

                    unregister_ws_connected(aid, conn_gen=conn_gen)
                with contextlib.suppress(Exception):
                    from xoso66_minigame_ws_worker import mark_ws_prune_unconnected

                    mark_ws_prune_unconnected(aid)
            await _abort_ws_session(
                ws=ws,
                sock=sock,
                reader_task=None,
                ping_stop=ping_stop,
                ping_task=ping_task,
                tag=username,
            )
            ws = None
            sock = None
            ping_task = None

        if deadline and time.time() >= deadline:
            break
        if stopping():
            break
        if duration_sec <= 0:
            # Nick mới / subscribe đụng stampede: mở lại 2s, không chờ 20s.
            sleep_iv = reconnect_interval
            if quick_retry or (
                not had_live_ws and not need_minigame_refresh
            ):
                sleep_iv = 2.0
            remain = sleep_iv - (time.time() - connect_started_at)
            if not had_live_ws:
                print(
                    f"[WS] [{username}] Closed — reconnect in {max(0.0, remain):.0f}s",
                    flush=True,
                )
            await _sleep_until_reconnect(
                connect_started_at,
                interval_sec=sleep_iv,
                is_stopping=stopping,
            )
        else:
            break

    return state


async def _amain(args: argparse.Namespace) -> int:
    account_id = resolve_account_id(username=args.username, account_id=args.account)
    ws_tok = (args.ws_token or os.environ.get("XOSO66_WS_TOKEN") or "").strip() or None
    watch_ids = parse_watch_game_ids(args.watch_games)
    state = await listen_minigame_ws(
        {},
        account_id,
        duration_sec=args.duration,
        game_key=args.game,
        refresh_before_connect=not args.ws_only and not ws_tok,
        ws_token_override=ws_tok,
        verbose=not args.quiet,
        game_watch=not args.jackpot_table,
        watch_game_ids=watch_ids,
        subscribe_spec=args.subscribe or None,
        ping_game_id=args.ping_game_id,
        debug_ws=args.debug_ws,
        subscribe_individual=args.subscribe_individual,
        save_jackpot=not args.no_save_jackpot,
    )
    print(
        f"\n[WS] Total: messages={state.msg_count}, jackpot_updates={state.jackpot_updates}, "
        f"games={len(state.by_game)}",
        flush=True,
    )
    if state.by_game:
        print(format_jackpot_table(state), flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="XOSO66 mini-game WebSocket")
    ap.add_argument("-u", "--username", default="", help="username trong DB")
    ap.add_argument("-a", "--account", default="", help="account id (acc1) — thay cho -u")
    ap.add_argument("--duration", type=float, default=0, help="giây chạy (0 = đến Ctrl+C)")
    ap.add_argument("--game", default="taixiu_dai_loc")
    ap.add_argument("--ws-only", action="store_true", help="không refresh user-token, chỉ getToken")
    ap.add_argument(
        "--jackpot-table",
        action="store_true",
        help="chế độ cũ: in bảng jackpot thay vì KẾT QUẢ/PHIÊN MỚI",
    )
    ap.add_argument("--quiet", action="store_true", help="ít log raw (với --jackpot-table)")
    ap.add_argument("--ws-token", default="", help="bỏ qua getToken — dùng token WS tay")
    ap.add_argument(
        "--watch-games",
        default="all",
        help='game_id theo dõi, VD: "9" (mặc định debug), "6" hoặc all = 6 game',
    )
    ap.add_argument(
        "--subscribe",
        default="",
        help='subscribe sau connect, VD: "0,9,[17,18,19,2]"',
    )
    ap.add_argument(
        "--ping-game-id",
        type=int,
        default=None,
        help="ping cố định 1 game_id (thử: 17)",
    )
    ap.add_argument(
        "--debug-ws",
        action="store_true",
        help="in raw g_game_info / g_open_info (thử 1 game lạ)",
    )
    ap.add_argument(
        "--subscribe-individual",
        action="store_true",
        help="subscribe từng game_id (mặc định: 0 + [9,17,18,19,2] — 5 game có hũ)",
    )
    ap.add_argument(
        "--no-save-jackpot",
        action="store_true",
        help="không ghi file minigame_jackpots.json",
    )
    args = ap.parse_args()
    if not (args.username or "").strip() and not (args.account or "").strip():
        from xoso66_config_util import ws_default_username

        default_u = ws_default_username()
        if default_u:
            args.username = default_u
            print(f"[WS] Username mặc định: {default_u}", flush=True)
        else:
            args.username = input("Username: ").strip()
    if not resolve_account_id(username=args.username, account_id=args.account):
        print("Cần -u username hoặc -a account.", file=sys.stderr)
        return 1
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        print(f"\nDừng.", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
