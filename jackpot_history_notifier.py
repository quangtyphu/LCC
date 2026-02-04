import time
import threading
import requests
import os

from pathlib import Path

from game_api_helper import game_request_with_retry
from telegram_notifier import send_telegram
import constants

API_BASE = "http://127.0.0.1:3000"
JACKPOT_API_URL = "https://wtx.tele68.com/v1/tx/jackpot-history"

_notified_sessions: set[str] = set()
_last_cleanup_ts = 0.0
_notify_lock = threading.Lock()
_NOTIFY_CACHE_DIR = Path(__file__).resolve().parent / "jackpot_notified"


def _cleanup_notified_sessions():
    global _last_cleanup_ts
    now = time.time()
    if now - _last_cleanup_ts < 300:
        return
    _last_cleanup_ts = now
    if len(_notified_sessions) > 500:
        _notified_sessions.clear()
    _cleanup_lock_files()


def _session_lock_path(session_key: str) -> Path:
    safe_key = "".join(ch for ch in str(session_key) if ch.isalnum() or ch in ("-", "_"))
    if not safe_key:
        safe_key = "unknown"
    return _NOTIFY_CACHE_DIR / f"{safe_key}.lock"


def _try_create_session_lock(session_key: str) -> bool:
    try:
        _NOTIFY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        lock_path = _session_lock_path(session_key)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(str(time.time()))
        return True
    except FileExistsError:
        return False
    except Exception as e:
        print(f"⚠️ Lỗi tạo lock jackpot: {e}", flush=True)
        return True


def _remove_session_lock(session_key: str) -> None:
    try:
        _session_lock_path(session_key).unlink(missing_ok=True)
    except Exception:
        pass


def _cleanup_lock_files(max_files: int = 1000, max_age_seconds: int = 60 * 60 * 24) -> None:
    try:
        if not _NOTIFY_CACHE_DIR.exists():
            return
        now = time.time()
        lock_files = [p for p in _NOTIFY_CACHE_DIR.iterdir() if p.is_file()]
        for path in lock_files:
            try:
                if now - path.stat().st_mtime > max_age_seconds:
                    path.unlink(missing_ok=True)
            except Exception:
                continue
        lock_files = [p for p in _NOTIFY_CACHE_DIR.iterdir() if p.is_file()]
        if len(lock_files) > max_files:
            lock_files.sort(key=lambda p: p.stat().st_mtime)
            for path in lock_files[: len(lock_files) - max_files]:
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    continue
    except Exception:
        return


def _normalize_result_code(data: dict) -> int | None:
    if not isinstance(data, dict):
        return None
    result = data.get("result")
    if isinstance(result, (int, float)):
        return int(result)
    if isinstance(result, str):
        s = result.strip()
        if s.isdigit():
            return int(s)
    dices = data.get("dices") or data.get("dice")
    if isinstance(dices, (list, tuple)) and len(dices) == 3:
        try:
            joined = int("".join(str(int(x)) for x in dices))
            return joined
        except Exception:
            return None
    return None


def _fetch_jackpot_history(username: str, limit: int = 6) -> list | None:
    resp = game_request_with_retry(username, "GET", JACKPOT_API_URL, params={"limit": limit})
    if not resp or resp.status_code != 200:
        print(f"❌ [{username}] Lỗi jackpot-history: {resp.status_code if resp else 'No response'}", flush=True)
        return None
    try:
        data = resp.json()
    except Exception as e:
        print(f"❌ [{username}] Lỗi parse jackpot-history: {e}", flush=True)
        return None
    if isinstance(data, dict):
        details = data.get("details") or data.get("data") or data.get("items")
        if isinstance(details, list):
            return details
    if isinstance(data, list):
        return data
    return None


def _find_detail_by_session(details: list, session_id) -> dict | None:
    if not details:
        return None
    for item in details:
        if not isinstance(item, dict):
            continue
        sid = item.get("session_id") or item.get("sessionId") or item.get("id")
        if sid is not None and str(sid) == str(session_id):
            return item
    return details[0] if isinstance(details[0], dict) else None


def _sum_bets_from_items(items: list, session_id) -> float | None:
    total = 0.0
    matched = False
    for item in items:
        if not isinstance(item, dict):
            continue
        sid = item.get("session_id") or item.get("sessionId") or item.get("session")
        if sid is not None and str(sid) != str(session_id):
            continue
        amount = item.get("amount") or item.get("money") or item.get("bet") or 0
        try:
            total += float(amount)
            matched = True
        except Exception:
            continue
    return total if matched else None


def _extract_total_from_response(data, session_id) -> float | None:
    if isinstance(data, dict):
        for key in ("totalBet", "total_bet", "total", "sum", "amount", "money"):
            if key in data:
                try:
                    return float(data[key])
                except Exception:
                    pass
        items = data.get("items") or data.get("data") or data.get("rows")
        if isinstance(items, list):
            return _sum_bets_from_items(items, session_id)
    if isinstance(data, list):
        return _sum_bets_from_items(data, session_id)
    return None


def _fetch_total_bet_for_session(session_id) -> float | None:
    try:
        local = constants.session_bet_totals.get(session_id) or constants.session_bet_totals.get(str(session_id))
        if isinstance(local, dict):
            local_total = local.get("total")
            if isinstance(local_total, (int, float)):
                return float(local_total)
    except Exception:
        pass

    endpoints = [
        (f"{API_BASE}/api/bet-history/summary", {"session_id": session_id}),
        (f"{API_BASE}/api/bet-history/total", {"session_id": session_id}),
        (f"{API_BASE}/api/bet-history", {"session_id": session_id}),
        (f"{API_BASE}/api/bet-history/session/{session_id}", None),
    ]
    for url, params in endpoints:
        try:
            r = requests.get(url, params=params, timeout=5)
            if r.status_code != 200:
                continue
            data = r.json()
            total = _extract_total_from_response(data, session_id)
            if total is not None:
                return total
        except Exception:
            continue
    return None


def check_and_notify_jackpot(username: str, session_data: dict):
    if not isinstance(session_data, dict):
        return
    session_id = (
        session_data.get("session_id")
        or session_data.get("sessionId")
        or session_data.get("id")
    )
    if session_id is None:
        return
    result_code = _normalize_result_code(session_data)
    if result_code not in (111, 666):
        return

    session_key = str(session_id)
    with _notify_lock:
        if session_key in _notified_sessions:
            return
        if not _try_create_session_lock(session_key):
            return
        _notified_sessions.add(session_key)

    details = _fetch_jackpot_history(username, limit=6)
    detail = _find_detail_by_session(details, session_id) if details else None
    result_text = None
    jackpot_amount = None
    if isinstance(detail, dict):
        result_text = detail.get("result")
        jackpot_amount = detail.get("jackpotAmount") or detail.get("jackpot_amount")
        if session_id is None:
            session_id = detail.get("session_id") or detail.get("sessionId") or detail.get("id")

    total_bet = _fetch_total_bet_for_session(session_id)

    result_display = result_text or str(result_code)
    jackpot_display = f"{jackpot_amount:,.2f}" if isinstance(jackpot_amount, (int, float)) else str(jackpot_amount or "N/A")
    total_display = f"{total_bet:,.2f}" if isinstance(total_bet, (int, float)) else str(total_bet or "N/A")

    msg = (
        f"Phiên: {session_id}\n"
        f"Nổ hũ: {result_display}\n"
        f"Số tiền: {jackpot_display}\n"
        f"Tổng cược: {total_display}"
    )

    if send_telegram(msg):
        _cleanup_notified_sessions()
    else:
        with _notify_lock:
            _notified_sessions.discard(session_key)
        _remove_session_lock(session_key)
