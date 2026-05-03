import random
import time
from datetime import datetime

import requests
from zoneinfo import ZoneInfo

from constants import active_ws, load_config, save_config
from game_api_helper import game_request_with_retry

API_BASE = "http://127.0.0.1:3000"
TOP_BET_DAILY_URL = "https://gameapi.tele68.com/v1/event/top-bet/daily"
TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def _to_int(val, default=0):
    try:
        return int(float(val))
    except Exception:
        return default


def _get_mode_config(cfg: dict) -> dict:
    mode = cfg.get("AUTO_REFRESH_V2_FROM_TOP480", {})
    return mode if isinstance(mode, dict) else {}


def _is_enabled(cfg: dict) -> bool:
    mode = _get_mode_config(cfg)
    try:
        return int(mode.get("ENABLED", 0) or 0) == 1
    except Exception:
        return False


def _get_start_time(cfg: dict) -> str:
    mode = _get_mode_config(cfg)
    val = str(mode.get("START_TIME", "22:00") or "22:00").strip()
    return val if ":" in val else "22:00"


def _get_end_time(cfg: dict) -> str:
    mode = _get_mode_config(cfg)
    val = str(mode.get("END_TIME", "23:50") or "23:50").strip()
    return val if ":" in val else "23:50"


def _get_interval_seconds(cfg: dict) -> int:
    mode = _get_mode_config(cfg)
    try:
        val = int(mode.get("INTERVAL_SECONDS", 300) or 300)
        return max(30, val)
    except Exception:
        return 300


def _get_v2_count(cfg: dict) -> int:
    mode = _get_mode_config(cfg)
    try:
        val = int(mode.get("V2_COUNT", 8) or 8)
        return max(1, val)
    except Exception:
        return 8


def _parse_time_hm(s: str) -> tuple[int, int]:
    h, m = s.split(":", 1)
    return int(h), int(m)


def _is_within_refresh_window(cfg: dict) -> bool:
    """Chỉ refresh V2 khi giờ VN nằm trong [START_TIME, END_TIME] (cùng ngày, hoặc qua đêm nếu START > END)."""
    now = datetime.now(TZ)
    now_t = now.time()
    try:
        sh, sm = _parse_time_hm(_get_start_time(cfg))
        eh, em = _parse_time_hm(_get_end_time(cfg))
        start_t = now.replace(hour=sh, minute=sm, second=0, microsecond=0).time()
        end_t = now.replace(hour=eh, minute=em, second=0, microsecond=0).time()
    except Exception:
        start_t = now.replace(hour=20, minute=0, second=0, microsecond=0).time()
        end_t = now.replace(hour=23, minute=50, second=0, microsecond=0).time()
    if start_t <= end_t:
        return start_t <= now_t <= end_t
    return now_t >= start_t or now_t <= end_t


def _pick_probe_username(cfg: dict) -> str | None:
    # Ưu tiên 1 user bất kỳ đang WS (token/proxy còn sống); random để không dồn vào 1 acc.
    ws_users = [str(u or "").strip() for u in active_ws.keys() if str(u or "").strip()]
    if ws_users:
        return random.choice(ws_users)

    # Fallback về danh sách config nếu chưa có WS online.
    for key in ("PRIORITY_USERS", "PRIORITY_USERS_V2", "PRIORITY_USERS_V3"):
        lst = cfg.get(key, [])
        if not isinstance(lst, list):
            continue
        for u in lst:
            s = str(u or "").strip()
            if s:
                return s
    return None


def _fetch_money_bet_at_rank(username: str, date_str: str, rank: int) -> int:
    """
    Lấy moneyBet của hạng `rank` trên bảng top-bet ngày (mốc “top 500” khi rank=500).
    API chỉ cho limit<=200 / lần; dùng beforeId để lấy tiếp.
    """
    headers = {
        "origin": "https://lc79b.bet",
        "referer": "https://lc79b.bet/",
        "accept-language": "vi-VN,vi;q=0.9",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    }
    by_idx: dict[int, dict] = {}
    cursor = 1
    per_page = 200

    for _ in range(20):
        params = {"date": date_str, "limit": per_page}
        if cursor > 1:
            params["beforeId"] = cursor - 1
        resp = game_request_with_retry(
            username, "GET", TOP_BET_DAILY_URL, params=params, extra_headers=headers, timeout=20
        )
        if not resp or resp.status_code != 200:
            break
        try:
            data = resp.json()
        except Exception:
            break
        if not isinstance(data, list) or not data:
            break
        for row in data:
            idx_val = _to_int(row.get("idx"), 0)
            if idx_val > 0:
                by_idx[idx_val] = row
        if rank in by_idx:
            break
        if len(data) < per_page:
            break
        cursor += per_page

    return _to_int(by_idx.get(rank, {}).get("moneyBet"), 0)


def _merge_bet_total_item(by_user: dict[str, int], item: dict) -> None:
    username = str(item.get("username") or item.get("user") or "").strip()
    if not username:
        return
    total_day = _to_int(
        item.get("total_day")
        or item.get("today_bet")
        or item.get("todayBet")
        or item.get("totalBet"),
        0,
    )
    if total_day <= 0:
        return
    prev = by_user.get(username, 0)
    if total_day > prev:
        by_user[username] = total_day


def _load_bet_totals_rows() -> list[tuple[int, str]]:
    """
    Tổng cược ngày trên CMS (bet_totals), sort total_day.

    Gộp **mọi trang** (page=1..totalPages): chỉ lấy 10k/page thì user ~7M có thể
    không nằm top 10k — sẽ không bao giờ vào pool V2.
    """
    by_user: dict[str, int] = {}
    try:
        page = 1
        limit = 10000
        total_pages = 1
        while page <= total_pages and page <= 500:
            r = requests.get(
                f"{API_BASE}/api/bet-totals",
                params={"page": page, "limit": limit, "sort": "total_day"},
                timeout=30,
            )
            if r.status_code == 404 and page == 1:
                dp = 1
                dtotal = 1
                while dp <= dtotal and dp <= 500:
                    dr = requests.get(
                        f"{API_BASE}/api/bet-totals/daily",
                        params={"page": dp, "limit": limit},
                        timeout=30,
                    )
                    if dr.status_code != 200:
                        return [] if dp == 1 else [(t, u) for u, t in by_user.items()]
                    dpayload = dr.json()
                    if not isinstance(dpayload, dict):
                        return [] if dp == 1 else [(t, u) for u, t in by_user.items()]
                    dtotal = max(1, int(dpayload.get("totalPages") or 1))
                    ditems = dpayload.get("data")
                    if not isinstance(ditems, list):
                        break
                    for item in ditems:
                        if isinstance(item, dict):
                            _merge_bet_total_item(by_user, item)
                    if not ditems:
                        break
                    dp += 1
                break

            if r.status_code != 200:
                return [] if page == 1 else [(t, u) for u, t in by_user.items()]

            payload = r.json()
            if not isinstance(payload, dict):
                return [] if page == 1 else [(t, u) for u, t in by_user.items()]
            total_pages = max(1, int(payload.get("totalPages") or 1))
            items = payload.get("data")
            if not isinstance(items, list):
                break
            for item in items:
                if isinstance(item, dict):
                    _merge_bet_total_item(by_user, item)
            if not items:
                break
            page += 1
    except Exception:
        return [(t, u) for u, t in by_user.items()] if by_user else []

    return [(t, u) for u, t in by_user.items()]


def _fetch_v2_candidates_top500_split(
    threshold_top500: int,
    v2_count: int,
    rows: list[tuple[int, str]] | None = None,
) -> list[str]:
    """
    Ví dụ V2_COUNT=8, mốc hạng 500 trên top-bet ngày:
      - **4** user **< mốc**: sát mốc nhất (total_day cao nhất trong nhóm dưới).
      - **4** user **≥ mốc**: sort total_day **tăng dần** (không lấy từ đỉnh xuống).

    Thiếu người một nhánh: lấp từ nhánh kia / phần còn lại như cũ.
    V2_COUNT khác 8: n_below = min(4, N), n_above = N - n_below.
    """
    if threshold_top500 <= 0 or v2_count <= 0:
        return []
    if rows is None:
        rows = _load_bet_totals_rows()
    if not rows:
        return []
    n_below = min(4, v2_count)
    n_above = max(0, v2_count - n_below)

    above = [x for x in rows if x[0] >= threshold_top500]
    below = [x for x in rows if x[0] < threshold_top500]
    # ≥ mốc: từ nhỏ đến lớn (4 → 5 → 6 …)
    above_asc = sorted(above, key=lambda x: (x[0], x[1]))
    # < mốc: cao → thấp (sát mốc trước)
    below_desc = sorted(below, key=lambda x: (-x[0], x[1]))

    out: list[str] = []
    seen: set[str] = set()

    for _, u in below_desc:
        if len(out) >= n_below:
            break
        if u in seen:
            continue
        seen.add(u)
        out.append(u)

    for _, u in above_asc:
        if len(out) >= n_below + n_above:
            break
        if u in seen:
            continue
        seen.add(u)
        out.append(u)

    if len(out) < v2_count:
        rest_below = sorted((x for x in below if x[1] not in seen), key=lambda x: (-x[0], x[1]))
        for _, u in rest_below:
            if len(out) >= v2_count:
                break
            seen.add(u)
            out.append(u)

    if len(out) < v2_count:
        rest_above = sorted((x for x in above if x[1] not in seen), key=lambda x: (x[0], x[1]))
        for _, u in rest_above:
            if len(out) >= v2_count:
                break
            seen.add(u)
            out.append(u)

    return out[:v2_count]


def _normalize_v2_list(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    out = []
    seen = set()
    for x in raw:
        u = str(x or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def refresh_v2_once_from_top480() -> None:
    cfg = load_config()
    if not cfg or not _is_enabled(cfg):
        return

    # Chỉ chạy trong khung [START_TIME, END_TIME] (giờ VN).
    if not _is_within_refresh_window(cfg):
        print(
            f"[V2_REFRESH] Bỏ qua: ngoài khung giờ {_get_start_time(cfg)}–{_get_end_time(cfg)} (VN)",
            flush=True,
        )
        return

    # Rule cứng: bật AUTO_REFRESH_V2_FROM_TOP480 => NEW_STRATEGY phải tắt.
    new_strategy_cfg = cfg.get("NEW_STRATEGY", {})
    if not isinstance(new_strategy_cfg, dict):
        new_strategy_cfg = {}
    try:
        new_strategy_enabled = int(new_strategy_cfg.get("ENABLED", 0) or 0) == 1
    except Exception:
        new_strategy_enabled = False
    if new_strategy_enabled:
        new_strategy_cfg["ENABLED"] = 0
        cfg["NEW_STRATEGY"] = new_strategy_cfg
        save_config(cfg)
        cfg = load_config() or cfg

    probe_user = _pick_probe_username(cfg)
    if not probe_user:
        print("[V2_REFRESH] Bỏ qua: không có probe user (WS/config) để gọi top-bet ngày", flush=True)
        return

    today_str = datetime.now(TZ).strftime("%Y-%m-%d")
    threshold_top500 = _fetch_money_bet_at_rank(probe_user, today_str, 500)
    if threshold_top500 <= 0:
        print(
            f"[V2_REFRESH] Bỏ qua: không lấy được mốc top 500 (probe={probe_user}, date={today_str})",
            flush=True,
        )
        return

    v2_count = _get_v2_count(cfg)
    rows = _load_bet_totals_rows()
    n_ge = sum(1 for t, _ in rows if t >= threshold_top500)
    n_lt = sum(1 for t, _ in rows if t < threshold_top500)
    print(
        f"[V2_REFRESH] CMS /api/bet-totals (sort=total_day) — "
        f"{len(rows)} user total_day>0 | ≥mốc={n_ge} | <mốc={n_lt} | "
        f"mốc_hạng500={threshold_top500:,}đ | probe={probe_user}",
        flush=True,
    )
    new_v2 = _fetch_v2_candidates_top500_split(threshold_top500, v2_count, rows)
    if not new_v2:
        print("[V2_REFRESH] Bỏ qua: bet-totals không đủ dữ liệu để ghép 4 dưới / 4 trên mốc (top 500)", flush=True)
        return

    row_by_u = {u: t for t, u in rows}
    print(
        "[V2_REFRESH] V2 + total_day CMS: "
        + ", ".join(f"{u}={row_by_u.get(u, 0):,}đ" for u in new_v2),
        flush=True,
    )

    old_v2 = _normalize_v2_list(cfg.get("PRIORITY_USERS_V2", []))
    effective_v2 = new_v2[:v2_count]
    added = [u for u in effective_v2 if u not in old_v2]
    removed = [u for u in old_v2 if u not in effective_v2]

    strategy_changed = int(cfg.get("ASSIGN_STRATEGY", 0) or 0) != 12

    if not added and not removed and not strategy_changed:
        iv = _get_interval_seconds(cfg)
        print(
            f"[V2_REFRESH] Đã check top-bet + bet-totals — V2 không đổi ({len(effective_v2)} user) | "
            f"mốc hạng500={threshold_top500:,}đ | chu kỳ {iv}s",
            flush=True,
        )
        return

    cfg["PRIORITY_USERS_V2"] = effective_v2
    cfg["ASSIGN_STRATEGY"] = 12
    if save_config(cfg):
        added_text = ", ".join(added) if added else "không có"
        removed_text = ", ".join(removed) if removed else "không có"
        if added or removed:
            print(f"Thêm user {added_text} vào -- Loại user {removed_text} khỏi list", flush=True)


def auto_refresh_v2_from_top480_scheduler() -> None:
    """Mỗi INTERVAL_SECONDS (config) gọi refresh: top-bet hạng 500 + bet-totals → PRIORITY_USERS_V2."""
    last_run = 0.0
    while True:
        try:
            cfg = load_config()
            interval = _get_interval_seconds(cfg)
            enabled = bool(cfg and _is_enabled(cfg))
            if enabled and time.time() - last_run >= interval:
                refresh_v2_once_from_top480()
                last_run = time.time()
        except Exception as e:
            print(f"[V2_REFRESH] Lỗi scheduler: {e}", flush=True)
        time.sleep(5)

