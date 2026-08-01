"""
Theo dõi lệch tổng cược Tài / Xỉu theo phiên và P/L giả định khi có kết quả.

File tai_xiu_skew_stats.json chỉ lưu {"cumulative": ...} (tích lũy lỗ+/lãi−).
Phiên đang chờ KQ giữ trong RAM, không ghi disk.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

STATS_FILE = os.path.join(os.path.dirname(__file__), "tai_xiu_skew_stats.json")

_pending_sessions: Dict[str, dict] = {}
_finalized_session_ids: set[str] = set()
_finalized_session_order: Deque[str] = deque()
_FINALIZED_IDS_MAX = 500
_lock = threading.Lock()


def _mark_session_finalized(sid: str) -> None:
    if sid in _finalized_session_ids:
        return
    _finalized_session_ids.add(sid)
    _finalized_session_order.append(sid)
    while len(_finalized_session_order) > _FINALIZED_IDS_MAX:
        old = _finalized_session_order.popleft()
        _finalized_session_ids.discard(old)


def dices_to_winner(dices: Any) -> Optional[str]:
    if not isinstance(dices, (list, tuple)) or len(dices) != 3:
        return None
    try:
        total = sum(int(x) for x in dices)
    except (TypeError, ValueError):
        return None
    if total <= 10:
        return "XIU"
    if total >= 11:
        return "TAI"
    return None


def _session_pnl(total_tai: int, total_xiu: int, winner: str) -> int:
    if winner == "TAI":
        return int(total_tai) - int(total_xiu)
    if winner == "XIU":
        return int(total_xiu) - int(total_tai)
    return 0


def _load_cumulative() -> int:
    if not os.path.isfile(STATS_FILE):
        return 0
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return 0
    if isinstance(raw, dict):
        cum = int(raw.get("cumulative") or 0)
        if "pending" in raw or "history" in raw:
            _save_cumulative(cum)
        return cum
    if isinstance(raw, (int, float)):
        _save_cumulative(int(raw))
        return int(raw)
    return 0


def _save_cumulative(cumulative: int) -> None:
    tmp = STATS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"cumulative": int(cumulative)}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATS_FILE)
    except Exception as e:
        print(f"⚠️ tai_xiu_skew_stats ghi file lỗi: {e}", flush=True)


def register_session(session_id: Any, total_tai: int, total_xiu: int) -> None:
    if session_id is None:
        return
    sid = str(session_id)
    if sid in _finalized_session_ids:
        return
    old = _pending_sessions.get(sid) or {}
    if old.get("finalized"):
        return
    skew = int(total_xiu) - int(total_tai)
    _pending_sessions[sid] = {
        "total_tai": int(total_tai),
        "total_xiu": int(total_xiu),
        "skew": skew,
        "winner": old.get("winner"),
        "dices": old.get("dices"),
        "registered_at": time.time(),
        "needs_retry": False,
        "finalized": False,
    }


def _resolve_session_id(session_id: Any) -> Optional[str]:
    if session_id is not None:
        return str(session_id)
    open_pending = {
        k: v
        for k, v in _pending_sessions.items()
        if not v.get("finalized") and not v.get("winner")
    }
    if not open_pending:
        return None
    if len(open_pending) == 1:
        return next(iter(open_pending))
    return min(
        open_pending.keys(),
        key=lambda k: float(open_pending[k].get("registered_at") or 0),
    )


def note_session_winner(
    session_id: Any,
    dices: Any,
    *,
    winner: Optional[str] = None,
    source: str = "",
) -> None:
    sid = _resolve_session_id(session_id)
    if sid is None:
        return
    w = (winner or "").upper() if winner else dices_to_winner(dices)
    if w not in ("TAI", "XIU"):
        return
    with _lock:
        if sid in _finalized_session_ids:
            return
        entry = _pending_sessions.setdefault(
            sid,
            {
                "total_tai": 0,
                "total_xiu": 0,
                "skew": 0,
                "registered_at": time.time(),
                "needs_retry": False,
                "finalized": False,
            },
        )
        if entry.get("finalized"):
            return
        if entry.get("winner"):
            return
        entry["winner"] = w
        if dices is not None:
            entry["dices"] = list(dices) if isinstance(dices, (list, tuple)) else dices
        if source:
            entry["result_source"] = source
    try_finalize(sid)


def _fill_totals_from_constants(entry: dict, session_id: str) -> bool:
    try:
        import constants

        totals = constants.session_bet_totals.get(session_id)
        if not totals and session_id.isdigit():
            totals = constants.session_bet_totals.get(int(session_id))
        if not totals:
            return False
        entry["total_tai"] = int(totals.get("tai") or 0)
        entry["total_xiu"] = int(totals.get("xiu") or 0)
        entry["skew"] = entry["total_xiu"] - entry["total_tai"]
        return True
    except Exception:
        return False


def try_finalize(session_id: Any) -> bool:
    if session_id is None:
        return False
    sid = str(session_id)

    with _lock:
        if sid in _finalized_session_ids:
            return True
        entry = _pending_sessions.get(sid)
        if not entry:
            return False
        if entry.get("finalized"):
            return True

        if not entry.get("total_tai") and not entry.get("total_xiu"):
            _fill_totals_from_constants(entry, sid)
        winner = entry.get("winner")
        if winner not in ("TAI", "XIU"):
            return False

        total_tai = int(entry.get("total_tai") or 0)
        total_xiu = int(entry.get("total_xiu") or 0)
        if total_tai == 0 and total_xiu == 0:
            return False
        skew = int(entry.get("skew") if entry.get("skew") is not None else (total_xiu - total_tai))
        dices = entry.get("dices")

        entry["finalized"] = True
        _pending_sessions.pop(sid, None)
        _mark_session_finalized(sid)

        if skew == 0:
            return True

        pnl = _session_pnl(total_tai, total_xiu, winner)
        cumulative = _load_cumulative()
        if pnl > 0:
            cumulative -= pnl
        elif pnl < 0:
            cumulative += abs(pnl)
        _save_cumulative(cumulative)

        winner_vn = "Tài" if winner == "TAI" else "Xỉu"
        pnl_label = "lãi" if pnl > 0 else "lỗ"
        print(
            f"📐 [Tài/Xỉu lệch] Phiên {sid}: Tài={total_tai:,} Xỉu={total_xiu:,} "
            f"lệch={skew:+d} | KQ={winner_vn} "
            f"(xúc sắc={dices}) | Phiên {pnl:+,} ({pnl_label}) | "
            f"Tích lũy lỗ+/lãi−={cumulative:,}",
            flush=True,
        )
    return True


def mark_needs_retry(session_id: Any) -> None:
    if session_id is None:
        return
    sid = str(session_id)
    if sid in _finalized_session_ids:
        return
    entry = _pending_sessions.get(sid)
    if not entry or entry.get("finalized"):
        return
    entry["needs_retry"] = True


def pop_retry_sessions() -> List[str]:
    out: List[str] = []
    for sid, entry in list(_pending_sessions.items()):
        if entry.get("needs_retry") and not entry.get("finalized"):
            entry["needs_retry"] = False
            out.append(sid)
    return out


def get_cumulative() -> int:
    return _load_cumulative()
