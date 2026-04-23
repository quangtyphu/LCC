import time
import threading
import os

from pathlib import Path

from game_api_helper import game_request_with_retry_ex
from telegram_notifier import send_telegram
from jackpot_session_db import upsert_jackpot_record
import constants

JACKPOT_API_URL = "https://wtx.tele68.com/v1/tx/jackpot-history"
SESSION_SUMMARY_URL = "https://wtx.tele68.com/v1/tx/session-summary"


def _game_total_one_side(overall: dict | None) -> float | None:
    if not isinstance(overall, dict):
        return None
    summaries = overall.get("betSummaries")
    if isinstance(summaries, list) and summaries:
        first = summaries[0]
        if isinstance(first, dict) and first.get("totalAmount") is not None:
            try:
                return float(first["totalAmount"])
            except (TypeError, ValueError):
                pass
    total = overall.get("totalAmount")
    if total is not None:
        try:
            return float(total) / 2.0
        except (TypeError, ValueError):
            pass
    return None


def _fetch_session_summary(username: str, session_id) -> tuple[dict | None, str | None]:
    """Trả về (data, failure_tag). failure_tag == 'proxy_exhausted' → caller không nên làm bước sau."""
    resp, tag = game_request_with_retry_ex(
        username,
        "GET",
        SESSION_SUMMARY_URL,
        params={"sessionId": session_id},
    )
    if tag == "proxy_exhausted":
        print(f"⚠️ [{username}] session-summary: proxy hết retry, bỏ các bước sau", flush=True)
        return None, "proxy_exhausted"
    if not resp or resp.status_code != 200:
        print(
            f"⚠️ [{username}] session-summary (jackpot DB) HTTP {resp.status_code if resp else 'none'}",
            flush=True,
        )
        return None, None
    try:
        data = resp.json()
    except Exception as e:
        print(f"⚠️ [{username}] session-summary parse: {e}", flush=True)
        return None, None
    return (data if isinstance(data, dict) else None), None


def _try_save_jackpot_db(
    username: str,
    session_id,
    session_data: dict,
    jackpot_amount,
    total_bet_session: float | None,
) -> str | None:
    """Ghi jackpot_session_records; không ném exception ra ngoài. Trả 'proxy_exhausted' nếu proxy hết retry."""
    try:
        summary, tag = _fetch_session_summary(username, session_id)
        if tag == "proxy_exhausted":
            return "proxy_exhausted"
        if not summary:
            return None
        overall = summary.get("overall")
        game_total = _game_total_one_side(overall)
        if not game_total or game_total <= 0:
            print(
                f"⚠️ [{username}] Không ghi jackpot DB: thiếu tổng cược game (session-summary)",
                flush=True,
            )
            return None

        my_bet = session_data.get("amount")
        try:
            my_bet_f = float(my_bet) if my_bet is not None else None
        except (TypeError, ValueError):
            my_bet_f = None
        if my_bet_f is None or my_bet_f <= 0:
            if isinstance(total_bet_session, (int, float)) and total_bet_session > 0:
                my_bet_f = float(total_bet_session)
        if my_bet_f is None or my_bet_f <= 0:
            print(
                f"⚠️ [{username}] Không ghi jackpot DB: thiếu tổng cược mình (amount / session_bet_totals)",
                flush=True,
            )
            return None
        if not isinstance(jackpot_amount, (int, float)) or float(jackpot_amount) <= 0:
            print(f"⚠️ [{username}] Không ghi jackpot DB: thiếu số hũ từ jackpot-history", flush=True)
            return None

        ja = float(jackpot_amount)
        amount_received = (my_bet_f * ja) / game_total
        ts_raw = summary.get("timestamp")
        jackpot_side = summary.get("resultTruyenThong")
        if not isinstance(jackpot_side, str):
            jackpot_side = None

        dices = summary.get("dices")
        if not isinstance(dices, (list, tuple)):
            dices = None
        dice_point = summary.get("point")
        if dice_point is not None:
            try:
                dice_point = int(dice_point)
            except (TypeError, ValueError):
                dice_point = None
        overall_total = overall.get("totalAmount") if isinstance(overall, dict) else None
        try:
            overall_total_f = float(overall_total) if overall_total is not None else None
        except (TypeError, ValueError):
            overall_total_f = None

        upsert_jackpot_record(
            int(session_id),
            username,
            my_bet_f,
            game_total,
            ja,
            amount_received,
            jackpot_side,
            ts_raw if isinstance(ts_raw, str) else None,
            api_username=username,
            dices=dices,
            dice_point=dice_point,
            overall_total_amount=overall_total_f,
        )
        print(
            f"✅ [{username}] Đã lưu jackpot_session_records phiên {session_id} "
            f"(nhận ~{amount_received:,.2f})",
            flush=True,
        )
    except Exception as e:
        print(f"⚠️ [{username}] Lỗi ghi jackpot DB: {e}", flush=True)
    return None

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


def _fetch_jackpot_history(username: str, limit: int = 6) -> tuple[list | None, str | None]:
    """(details, failure_tag). Chỉ failure_tag == 'proxy_exhausted' là cần dừng hẳn pipeline."""
    resp, tag = game_request_with_retry_ex(username, "GET", JACKPOT_API_URL, params={"limit": limit})
    if tag == "proxy_exhausted":
        print(f"❌ [{username}] jackpot-history: proxy hết retry, không gọi API khác", flush=True)
        return None, "proxy_exhausted"
    if not resp or resp.status_code != 200:
        print(f"❌ [{username}] Lỗi jackpot-history: {resp.status_code if resp else 'No response'}", flush=True)
        return None, tag or "error"
    try:
        data = resp.json()
    except Exception as e:
        print(f"❌ [{username}] Lỗi parse jackpot-history: {e}", flush=True)
        return None, "error"
    if isinstance(data, dict):
        details = data.get("details") or data.get("data") or data.get("items")
        if isinstance(details, list):
            return details, None
    if isinstance(data, list):
        return data, None
    return None, None


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


def _fetch_total_bet_for_session(session_id) -> float | None:
    """Chỉ còn nguồn in-memory khi bot WS chạy (CMS đã bỏ bet_history)."""
    try:
        local = constants.session_bet_totals.get(session_id) or constants.session_bet_totals.get(str(session_id))
        if isinstance(local, dict):
            local_total = local.get("total")
            if isinstance(local_total, (int, float)):
                return float(local_total)
    except Exception:
        pass
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

    details, _ = _fetch_jackpot_history(username, limit=6)
    if not details:
        _remove_session_lock(session_key)
        return

    with _notify_lock:
        _notified_sessions.add(session_key)

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

    print(
        f"🎰 [{username}] Nổ hũ 111/666 phiên {session_id} → ghi DB + gửi Telegram",
        flush=True,
    )
    save_tag = _try_save_jackpot_db(username, session_id, session_data, jackpot_amount, total_bet)
    if save_tag == "proxy_exhausted":
        with _notify_lock:
            _notified_sessions.discard(session_key)
        _remove_session_lock(session_key)
        return

    if send_telegram(msg):
        _cleanup_notified_sessions()
    else:
        with _notify_lock:
            _notified_sessions.discard(session_key)
        _remove_session_lock(session_key)
