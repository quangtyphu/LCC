# jackpot_night_extend.py
# - ENABLED: cả ngày — ngoài PAUSE chỉ cược khi TxMini > JACKPOT_THRESHOLD; CACHE_SECONDS giới hạn tần suất API;
#   trong PAUSE của TIME_WINDOWS luôn nghỉ (không overlay template).
# - PERIODIC_CHECK_ENABLED: (tuỳ chọn) cổng jackpot + overlay PAUSE theo template khi ENABLED tắt; interval PERIODIC_CHECK_SECONDS.
# - Nổ hũ 111/666 → dừng cược tới khi TxMini lại > ngưỡng (trừ POST_JACKPOT_EXTRA_ROUNDS grace).
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any

from game_api_helper import game_request_with_retry_ex

_STATE_PATH = os.path.join(os.path.dirname(__file__), "jackpot_night_extend_state.json")
_TOP_JACKPOT_URL = "https://gameapi.tele68.com/v1/top-jack-pot/data-update"

_last_jackpot_fetch_mono: float = 0.0


def _fmt_vn_int(n: Any) -> str:
    """Định dạng số nguyên kiểu VN: 19962441 → 19.962.441."""
    if n is None:
        return "?"
    try:
        x = int(n)
    except (TypeError, ValueError):
        return str(n)
    neg = x < 0
    x = abs(x)
    s = str(x)
    parts: list[str] = []
    while s:
        parts.append(s[-3:])
        s = s[:-3]
    body = ".".join(reversed(parts))
    return f"-{body}" if neg else body


def _default_jcfg(cfg: dict) -> dict:
    return cfg.get("JACKPOT_NIGHT_EXTEND") or {}


def _jackpot_gate_enabled(j: dict) -> bool:
    return bool(j.get("ENABLED")) or bool(j.get("PERIODIC_CHECK_ENABLED"))


def _post_jackpot_extra_rounds(j: dict) -> int:
    """Số phiên được cược thêm sau nổ hũ (0 = tắt grace)."""
    try:
        return max(0, int(j.get("POST_JACKPOT_EXTRA_ROUNDS", 0)))
    except (TypeError, ValueError):
        return 0


def _grace_rounds_left(st: dict) -> int:
    try:
        return max(0, int(st.get("post_jackpot_rounds_left") or 0))
    except (TypeError, ValueError):
        return 0


def post_jackpot_grace_active(cfg: dict | None = None) -> bool:
    if cfg is None:
        from constants import load_config

        cfg = load_config()
    j = _default_jcfg(cfg)
    if _post_jackpot_extra_rounds(j) <= 0:
        return False
    return _grace_rounds_left(read_state()) > 0


def _state_defaults() -> dict[str, Any]:
    return {
        "last_check_date": None,
        "extend_until": None,
        "periodic_extend_until": None,
        "periodic_bet_allowed": False,
        "last_periodic_check_ts": None,
        "cancelled": False,
        "last_txmini": None,
        "last_check_error": None,
        "last_periodic_txmini": None,
        "last_periodic_error": None,
        "post_jackpot_rounds_left": 0,
        "post_jackpot_last_session_id": None,
    }


def read_state() -> dict[str, Any]:
    st = _state_defaults()
    try:
        if os.path.isfile(_STATE_PATH):
            with open(_STATE_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                st.update(raw)
    except Exception as e:
        print(f"⚠️ [jackpot_night_extend] Đọc state lỗi: {e}", flush=True)
    return st


def write_state(st: dict[str, Any]) -> None:
    try:
        tmp = _STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _STATE_PATH)
    except Exception as e:
        print(f"⚠️ [jackpot_night_extend] Ghi state lỗi: {e}", flush=True)


def cancel_extend_on_jackpot_hit(cfg: dict | None = None) -> None:
    """Gọi khi 111/666: jackpot về ~0 → dừng cược tới khi TxMini lại > ngưỡng."""
    if cfg is None:
        from constants import load_config

        cfg = load_config()
    j = _default_jcfg(cfg)
    extra = _post_jackpot_extra_rounds(j)

    st = read_state()
    st["extend_until"] = None
    st["periodic_extend_until"] = None
    st["periodic_bet_allowed"] = False
    st["cancelled"] = True
    st["post_jackpot_last_session_id"] = None
    if extra > 0:
        st["post_jackpot_rounds_left"] = extra
        write_state(st)
        print(
            f"🎰 [jackpot_night_extend] Nổ hũ → dừng theo ngưỡng hũ; grace {extra} phiên cược thêm.",
            flush=True,
        )
    else:
        st["post_jackpot_rounds_left"] = 0
        write_state(st)
        print("🎰 [jackpot_night_extend] Nổ hũ → dừng cược theo ngưỡng jackpot.", flush=True)


def consume_post_jackpot_round(session_id: Any, cfg: dict | None = None) -> None:
    """
    Gọi một lần mỗi new-session (ws_events). Trừ grace nếu đang còn phiên grace.
    """
    if session_id is None:
        return
    if cfg is None:
        from constants import load_config

        cfg = load_config()
    j = _default_jcfg(cfg)
    if _post_jackpot_extra_rounds(j) <= 0:
        return

    sid = str(session_id)
    st = read_state()
    left = _grace_rounds_left(st)
    if left <= 0:
        return
    if st.get("post_jackpot_last_session_id") == sid:
        return

    st["post_jackpot_last_session_id"] = sid
    st["post_jackpot_rounds_left"] = left - 1
    write_state(st)
    remaining = st["post_jackpot_rounds_left"]
    print(
        f"🎰 [jackpot_night_extend] Grace phiên {sid}: còn {remaining} phiên sau nổ hũ.",
        flush=True,
    )


def _find_template_window(cfg: dict, start: str, end: str) -> dict | None:
    for w in cfg.get("TIME_WINDOWS") or []:
        if w.get("start") == start and w.get("end") == end and not w.get("PAUSE"):
            return dict(w)
    return None


def apply_pause_overlay_if_eligible(cfg: dict, base_window: dict, now_dt: datetime) -> dict:
    """
    ENABLED: PAUSE tuyệt đối (không overlay).
    Chỉ PERIODIC_CHECK_ENABLED (khi ENABLED tắt): PAUSE + periodic_bet_allowed → template.
    """
    j = _default_jcfg(cfg)
    if bool(j.get("ENABLED")):
        return base_window
    periodic_on = bool(j.get("PERIODIC_CHECK_ENABLED"))
    if not periodic_on:
        return base_window
    if not base_window or not base_window.get("PAUSE"):
        return base_window
    st = read_state()
    if st.get("cancelled"):
        return base_window

    if not st.get("periodic_bet_allowed"):
        return base_window
    ts = str(j.get("TEMPLATE_START", "01:00"))
    te = str(j.get("TEMPLATE_END", "02:00"))
    template = _find_template_window(cfg, ts, te)
    if not template:
        return base_window
    return template


def _fetch_txmini_jackpot(username: str) -> tuple[int | None, str | None]:
    """Gọi API top-jack-pot qua proxy + JWT của user (game_request_with_retry_ex)."""
    resp, tag = game_request_with_retry_ex(
        username,
        "GET",
        _TOP_JACKPOT_URL,
        params=None,
        extra_headers=None,
        timeout=25,
    )
    if tag or resp is None:
        return None, tag or "error"
    if not (200 <= resp.status_code < 300):
        return None, f"http_{resp.status_code}"
    try:
        data = resp.json()
    except Exception:
        return None, "parse"
    if not isinstance(data, dict):
        return None, "parse"
    tx = data.get("TxMini")
    if not isinstance(tx, (list, tuple)) or len(tx) < 3:
        return None, "shape"
    v = tx[2]
    if v is None:
        return None, "null_jackpot"
    try:
        return int(v), None
    except (TypeError, ValueError):
        return None, "bad_value"


def try_run_daily_check(cfg: dict | None = None) -> None:
    """Giữ tên cho main.py. Cửa kéo đêm 2h–3h đã bỏ; jackpot khi bật ENABLED dùng refresh_jackpot_cache cả ngày."""
    return


def _periodic_threshold(j: dict) -> int:
    v = j.get("PERIODIC_JACKPOT_MIN")
    if v is not None:
        return int(v)
    return int(j.get("JACKPOT_THRESHOLD", 400_000_000))


def _cache_ttl_seconds(j: dict) -> float:
    if bool(j.get("ENABLED")):
        return max(1.0, float(j.get("CACHE_SECONDS", 300)))
    return max(30.0, float(j.get("PERIODIC_CHECK_SECONDS", 300)))


def refresh_jackpot_cache(cfg: dict | None = None, *, force: bool = False) -> bool:
    """
    Đọc TxMini (tối đa 1 lần mỗi CACHE_SECONDS nếu ENABLED, hoặc PERIODIC_CHECK_SECONDS nếu chỉ periodic).
    Cập nhật periodic_bet_allowed; TxMini > ngưỡng → bật cược và xóa cancelled.
    Trả về True nếu không cần gate hoặc đã gọi API xong (có probe); False nếu chưa có WS để đọc.
    """
    global _last_jackpot_fetch_mono
    if cfg is None:
        from constants import load_config

        cfg = load_config()
    j = _default_jcfg(cfg)
    if not _jackpot_gate_enabled(j):
        return True

    ttl = _cache_ttl_seconds(j)
    now_m = time.monotonic()
    if not force and (now_m - _last_jackpot_fetch_mono) < ttl:
        return True

    try:
        from session_game_total import pick_from_active_ws
    except ImportError:
        pick_from_active_ws = None  # type: ignore
    probe = pick_from_active_ws() if pick_from_active_ws else None
    if not probe:
        if force:
            print(
                "ℹ️ [jackpot_night_extend] Khởi động: chưa có user WS → chưa đọc TxMini (sẽ thử lại sau).",
                flush=True,
            )
        return False

    st = read_state()
    threshold = _periodic_threshold(j)
    val, err = _fetch_txmini_jackpot(probe)
    _last_jackpot_fetch_mono = time.monotonic()

    st["last_periodic_txmini"] = val
    st["last_periodic_error"] = err
    st["last_periodic_check_ts"] = time.time()

    prev_allowed = bool(st.get("periodic_bet_allowed"))

    if err is None and val is not None:
        if val > threshold:
            st["periodic_bet_allowed"] = True
            st["periodic_extend_until"] = None
            st["cancelled"] = False
            if _grace_rounds_left(st) > 0:
                st["post_jackpot_rounds_left"] = 0
                st["post_jackpot_last_session_id"] = None
            if force or not prev_allowed:
                mode = "ENABLED" if bool(j.get("ENABLED")) else "periodic"
                print(
                    f"✅ [jackpot_night_extend] ({mode}) TxMini={_fmt_vn_int(val)} > {_fmt_vn_int(threshold)} "
                    f"→ được cược (ngoài PAUSE, theo config).",
                    flush=True,
                )
        else:
            st["periodic_bet_allowed"] = False
            st["periodic_extend_until"] = None
            if force or prev_allowed:
                mode = "ENABLED" if bool(j.get("ENABLED")) else "periodic"
                print(
                    f"ℹ️ [jackpot_night_extend] ({mode}) TxMini={_fmt_vn_int(val)} ≤ {_fmt_vn_int(threshold)} → không cược.",
                    flush=True,
                )
    else:
        mode = "ENABLED" if bool(j.get("ENABLED")) else "periodic"
        print(
            f"⚠️ [jackpot_night_extend] ({mode}) Lỗi API (TxMini={_fmt_vn_int(val)}, err={err}) "
            f"→ giữ cờ cược: {prev_allowed}.",
            flush=True,
        )

    write_state(st)
    return True


def try_run_periodic_jackpot_check(cfg: dict | None = None) -> None:
    """Tương thích tên cũ — gọi refresh (tôn trọng cache TTL)."""
    refresh_jackpot_cache(cfg, force=False)


def jackpot_periodic_gate_allows_betting(cfg: dict) -> bool:
    """Ngoài PAUSE: bật ENABLED hoặc PERIODIC_CHECK thì cần TxMini > ngưỡng và chưa bị nổ hũ chặn."""
    j = _default_jcfg(cfg)
    if not _jackpot_gate_enabled(j):
        return True
    refresh_jackpot_cache(cfg, force=False)
    st = read_state()
    if _grace_rounds_left(st) > 0:
        return True
    if st.get("cancelled"):
        return False
    if bool(st.get("periodic_bet_allowed")):
        return True
    # TxMini trong state đã > ngưỡng nhưng cờ vẫn tắt → lệch state / bỏ lỡ refresh; ép đọc API lại.
    threshold = _periodic_threshold(j)
    tx = st.get("last_periodic_txmini")
    err = st.get("last_periodic_error")
    try:
        tx_i = int(tx) if tx is not None else None
    except (TypeError, ValueError):
        tx_i = None
    if tx_i is not None and err is None and tx_i > threshold:
        refresh_jackpot_cache(cfg, force=True)
        st = read_state()
        if st.get("cancelled"):
            return False
        return bool(st.get("periodic_bet_allowed"))
    return False


def format_jackpot_gate_skip_reason(cfg: dict, *, action: str = "không chạy run_assigner") -> str:
    """
    Log chi tiết khi jackpot_periodic_gate_allows_betting(cfg) là False.
    Gọi ngay sau khi gate trả False — state đã được refresh_jackpot_cache cập nhật.
    """
    j = _default_jcfg(cfg)
    threshold = _periodic_threshold(j)
    st = read_state()
    tx = st.get("last_periodic_txmini")
    err = st.get("last_periodic_error")
    cancelled = bool(st.get("cancelled"))
    tail = f"→ {action}."
    ths = _fmt_vn_int(threshold)

    if cancelled:
        tx_disp = _fmt_vn_int(tx) if tx is not None else "?"
        return (
            f"⏸️ Jackpot: sau nổ hũ (cancelled); TxMini lưu={tx_disp}, ngưỡng={ths} {tail}"
        )
    if tx is not None:
        # Cổng cược: TxMini phải *strict* > threshold (xem refresh_jackpot_cache).
        txs = _fmt_vn_int(tx)
        if tx <= threshold:
            return f"⏸️ Jackpot: TxMini={txs} ≤ {ths} (cần > ngưỡng) {tail}"
        pa = st.get("periodic_bet_allowed")
        return (
            f"⏸️ Jackpot: TxMini={txs} > {ths} nhưng cờ periodic_bet_allowed={pa!r} (vẫn chặn). "
            f"Nghĩa là: số TxMini trong file state đã vượt ngưỡng nhưng cờ “được cược” chưa bật — "
            f"thường do state lệch/chưa ghi kịp; gate đã thử refresh bắt buộc nếu phát hiện. last_err={err} {tail}"
        )
    return f"⏸️ Jackpot: chưa đọc được TxMini (err={err}), ngưỡng={ths} {tail}"
