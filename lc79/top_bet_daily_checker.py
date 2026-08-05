# top_bet_daily_checker.py
"""
Script lấy TOP cược ngày (hạng 500) và chọn user V2 theo gap total_day − top500.

Chỉ xét user status Đang Chơi / Hết Tiền (cùng tuần/tháng).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set

import requests

from constants import load_config
from game_api_helper import game_request_with_retry

API_BASE = "http://127.0.0.1:3000"
STATUS_FETCH_URL = f"{API_BASE}/api/users/lc79-playing-or-out"
TARGET_TOP_IDX = 500
DEFAULT_TOP_500_OFFSET_VND = 500_000
_ALLOWED_STATUSES = frozenset({"Đang Chơi", "Hết Tiền"})

_TOP_BET_HEADERS = {
    "origin": "https://lc79b.bet",
    "referer": "https://lc79b.bet/",
    "accept-language": "vi-VN,vi;q=0.9,fr-FR;q=0.8,fr;q=0.7,en-US;q=0.6,en;q=0.5",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ),
}


def _to_int(val, default=0):
    try:
        return int(float(val))
    except Exception:
        return default


def _parse_users_payload(data) -> List[dict]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("data", "users", "items"):
            inner = data.get(key)
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
    return []


def _username_from_row(row: dict) -> str:
    u = row.get("username") or row.get("user") or row.get("name")
    return str(u).strip() if u else ""


def fetch_playing_or_out_usernames() -> Optional[Set[str]]:
    """
    Usernames CMS status Đang Chơi / Hết Tiền.
    None = lỗi gọi API (không prune / không chọn dựa trên status).
    """
    try:
        r = requests.get(STATUS_FETCH_URL, timeout=12)
        if r.status_code != 200:
            return None
        payload = r.json()
    except Exception:
        return None

    out: Set[str] = set()
    for row in _parse_users_payload(payload):
        status = str(row.get("status") or "").strip()
        if status not in _ALLOWED_STATUSES:
            continue
        u = _username_from_row(row)
        if u:
            out.add(u)
    return out


def _fetch_bet_total_rows() -> List[dict]:
    try:
        r = requests.get(
            f"{API_BASE}/api/bet-totals", params={"page": 1, "limit": 10000}, timeout=8
        )
        if r.status_code != 200:
            return []
        payload = r.json()
        items = payload.get("data") if isinstance(payload, dict) else payload
        return items if isinstance(items, list) else []
    except Exception:
        return []


def _fetch_all_cms_candidates() -> List[dict]:
    """User CMS: Đang Chơi / Hết Tiền và total_day > 0. API status lỗi → []."""
    allowed_names = fetch_playing_or_out_usernames()
    if allowed_names is None:
        return []
    allowed = {u.lower() for u in allowed_names}
    candidates: List[dict] = []
    for row in _fetch_bet_total_rows():
        username = str(row.get("username") or row.get("user") or "").strip()
        if not username or username.lower() not in allowed:
            continue
        total_day = _to_int(
            row.get("total_day")
            or row.get("today_bet")
            or row.get("todayBet")
            or row.get("total")
            or row.get("totalBet"),
            0,
        )
        if total_day > 0:
            candidates.append(
                {
                    "username": username,
                    "total_day": total_day,
                    "gap": 0,
                }
            )
    candidates.sort(key=lambda x: x["total_day"], reverse=True)
    return candidates


def _fetch_cms_total_day_by_username() -> Dict[str, int]:
    """Map username.lower → total_day từ bet-totals (không lọc status; dùng monitor gap)."""
    out: Dict[str, int] = {}
    for row in _fetch_bet_total_rows():
        username = str(row.get("username") or row.get("user") or "").strip()
        if not username:
            continue
        total_day = _to_int(
            row.get("total_day")
            or row.get("today_bet")
            or row.get("todayBet")
            or row.get("total")
            or row.get("totalBet"),
            0,
        )
        key = username.lower()
        if key not in out or total_day > out[key]:
            out[key] = total_day
    return out


DEFAULT_EXIT_GAP_MIN_VND = 200_000


def _mode_block() -> dict:
    cfg = load_config() or {}
    block = cfg.get("TOP_BET_DAILY_MODE")
    return block if isinstance(block, dict) else {}


def _threshold_offset_vnd() -> int:
    block = _mode_block()
    if block:
        return max(
            0,
            _to_int(
                block.get("THRESHOLD_OFFSET_VND", DEFAULT_TOP_500_OFFSET_VND),
                DEFAULT_TOP_500_OFFSET_VND,
            ),
        )
    return DEFAULT_TOP_500_OFFSET_VND


def _exit_gap_min_vnd() -> int:
    block = _mode_block()
    if block:
        return max(
            0,
            _to_int(block.get("EXIT_GAP_MIN_VND", DEFAULT_EXIT_GAP_MIN_VND), DEFAULT_EXIT_GAP_MIN_VND),
        )
    return DEFAULT_EXIT_GAP_MIN_VND


def effective_compare_moc(money_500_raw: int, offset_vnd: int | None = None) -> int:
    """Mốc so sánh CMS = moneyBet top500 − offset (mặc định 300k)."""
    off = DEFAULT_TOP_500_OFFSET_VND if offset_vnd is None else max(0, int(offset_vnd))
    return max(0, money_500_raw - off)


@dataclass
class TopBetPickResult:
    selected: List[dict] = field(default_factory=list)
    money_500: int = 0
    compare_moc: int = 0
    threshold_offset_vnd: int = DEFAULT_TOP_500_OFFSET_VND
    duoi_top: List[dict] = field(default_factory=list)
    tren_top: List[dict] = field(default_factory=list)
    rule: str = ""
    pick_reasons: Dict[str, str] = field(default_factory=dict)

    @property
    def below_500(self) -> List[dict]:
        """Dưới top500 = tổng cược > mốc (tên field cũ, cùng nghĩa duoi_top)."""
        return self.duoi_top

    @property
    def above_500(self) -> List[dict]:
        return self.tren_top

    @property
    def in_band(self) -> List[dict]:
        return self.duoi_top


def _dedupe_user_rows(rows: List[dict], limit: int) -> List[dict]:
    seen: set[str] = set()
    out: List[dict] = []
    for row in rows:
        u = row["username"]
        if u in seen:
            continue
        seen.add(u)
        out.append(row)
        if len(out) >= limit:
            break
    return out


def _leaderboard_by_idx(data: List[dict]) -> Dict[int, dict]:
    out: Dict[int, dict] = {}
    for entry in data:
        idx = _to_int(entry.get("idx"), 0)
        if idx > 0:
            out[idx] = entry
    return out


def _cms_by_username(candidates: List[dict]) -> Dict[str, dict]:
    return {c["username"].lower(): c for c in candidates}


def _row_from_leaderboard_entry(
    entry: dict,
    cms_by_name: Dict[str, dict],
    *,
    money_500: int,
) -> Optional[dict]:
    nick = str(entry.get("nickname") or "").strip()
    if not nick:
        return None
    idx = _to_int(entry.get("idx"), 0)
    mb = _to_int(entry.get("moneyBet"), 0)
    if nick.lower() in cms_by_name:
        row = dict(cms_by_name[nick.lower()])
    else:
        row = {
            "username": nick,
            "total_day": mb,
            "gap": max(0, money_500 - mb) if money_500 > mb else 0,
        }
    row["leaderboard_idx"] = idx
    row["leaderboard_money"] = mb
    return row


def _pick_v2_users_from_candidates(
    candidates: List[dict],
    *,
    money_500_raw: int,
    compare_moc: int,
    threshold_offset_vnd: int,
    user_count: int,
    leaderboard_data: Optional[List[dict]] = None,
) -> TopBetPickResult:
    """
    money_500_raw = moneyBet hạng 500; compare_moc = raw − offset (vd −300k).

    «Dưới top500»: total_day > compare_moc — 7 mức nhỏ nhất (gần mốc từ trên)
    «Trên top500»: total_day < compare_moc — 1 mức cao nhất (gần mốc từ dưới)
    """
    result = TopBetPickResult(
        money_500=money_500_raw,
        compare_moc=compare_moc,
        threshold_offset_vnd=threshold_offset_vnd,
    )
    if user_count <= 0:
        result.rule = "USER_COUNT=0"
        return result

    n_duoi = max(0, user_count - 1)
    moc = compare_moc
    by_idx = _leaderboard_by_idx(leaderboard_data or [])
    cms_by = _cms_by_username(candidates)

    duoi_pool = [c for c in candidates if moc > 0 and c["total_day"] > moc]
    duoi_pool.sort(key=lambda x: x["total_day"])
    result.duoi_top = list(duoi_pool)

    tren_pool = [c for c in candidates if moc > 0 and c["total_day"] < moc]
    tren_pool.sort(key=lambda x: x["total_day"], reverse=True)
    result.tren_top = list(tren_pool)

    seven_duoi = list(duoi_pool[:n_duoi])
    one_tren: List[dict] = list(tren_pool[:1])

    if len(seven_duoi) < n_duoi and by_idx:
        for idx in range(TARGET_TOP_IDX - n_duoi, TARGET_TOP_IDX):
            if len(seven_duoi) >= n_duoi:
                break
            if idx in by_idx:
                row = _row_from_leaderboard_entry(by_idx[idx], cms_by, money_500=moc)
                if row and row["username"].lower() not in {
                    r["username"].lower() for r in seven_duoi
                }:
                    seven_duoi.append(row)

    if not one_tren and by_idx:
        entry = by_idx.get(TARGET_TOP_IDX + 1)
        if entry:
            row = _row_from_leaderboard_entry(entry, cms_by, money_500=money_500_raw)
            if row:
                one_tren = [row]

    picked = _dedupe_user_rows(seven_duoi + one_tren, user_count)
    result.selected = picked

    if not picked:
        result.rule = "không chọn được user (CMS + leaderboard trống)"
        return result

    raw = money_500_raw
    off = threshold_offset_vnd
    if moc <= 0:
        result.rule = "thiếu mốc top 500"
    else:
        result.rule = (
            f"{n_duoi} user dưới top{TARGET_TOP_IDX} (cược > {moc:,} = top500 {raw:,} − {off:,}) + "
            f"1 user trên top{TARGET_TOP_IDX} (cược < {moc:,})"
        )

    duoi_names = {r["username"].lower() for r in seven_duoi}
    for row in picked:
        u = row["username"]
        td = row["total_day"]
        lb = row.get("leaderboard_idx")
        src = f", hạng API #{lb}" if lb else ""
        if u.lower() in duoi_names:
            result.pick_reasons[u] = (
                f"dưới top{TARGET_TOP_IDX}: {td:,} > {moc:,} (top500−{off:,}{src})"
            )
        else:
            result.pick_reasons[u] = (
                f"trên top{TARGET_TOP_IDX}: {td:,} < {moc:,} (gần mốc từ dưới{src})"
            )
    return result


def _pick_v2_users_by_gap_filter(
    candidates: List[dict],
    *,
    money_500_raw: int,
    max_entry_gap_vnd: int,
    user_count: int,
) -> TopBetPickResult:
    """
    Lọc user có total_day − top500 ≤ max_entry_gap_vnd, sort total_day giảm dần, lấy user_count.
    """
    result = TopBetPickResult(
        money_500=money_500_raw,
        threshold_offset_vnd=max_entry_gap_vnd,
    )
    if user_count <= 0:
        result.rule = "USER_COUNT=0"
        return result

    pool: List[dict] = []
    for row in candidates:
        gap = row["total_day"] - money_500_raw
        enriched = dict(row)
        enriched["gap"] = gap
        if gap <= max_entry_gap_vnd:
            pool.append(enriched)

    pool.sort(key=lambda x: x["total_day"], reverse=True)
    result.duoi_top = pool
    picked = pool[:user_count]
    result.selected = picked
    result.rule = (
        f"Lọc gap≤{max_entry_gap_vnd:,} (total_day−top{TARGET_TOP_IDX}), "
        f"sort total_day giảm dần, lấy tối đa {user_count}"
    )
    for row in picked:
        u = row["username"]
        result.pick_reasons[u] = (
            f"total_day={row['total_day']:,}, top500={money_500_raw:,}, gap={row['gap']:,}"
        )
    return result


def format_top_bet_gap_pick_report(pick: TopBetPickResult, *, user_count: int = 8) -> str:
    lines: List[str] = []
    raw = pick.money_500
    max_gap = pick.threshold_offset_vnd
    lines.append(f"📊 Mốc top {TARGET_TOP_IDX} (moneyBet) = {raw:,}")
    lines.append(
        f"   Lọc: total_day − top{TARGET_TOP_IDX} ≤ {max_gap:,} → {len(pick.duoi_top)} user CMS"
    )
    if pick.duoi_top:
        preview = ", ".join(
            f"{r['username']}({r['total_day']:,}, gap={r.get('gap', r['total_day'] - raw):,})"
            for r in pick.duoi_top[:15]
        )
        if len(pick.duoi_top) > 15:
            preview += f", … (+{len(pick.duoi_top) - 15})"
        lines.append(f"      {preview}")
    lines.append(f"   Quy tắc: {pick.rule}")
    lines.append(f"   Đã chọn {len(pick.selected)}/{user_count}:")
    for i, row in enumerate(pick.selected, 1):
        u = row["username"]
        reason = pick.pick_reasons.get(u, "")
        lines.append(
            f"      {i}. {u} — total_day={row['total_day']:,}"
            + (f" | {reason}" if reason else "")
        )
    return "\n".join(lines)


def v2_users_all_above_exit_gap(
    v2_usernames: List[str],
    money_500: int,
    exit_gap_min_vnd: int,
    cms_by_name: Optional[Dict[str, dict]] = None,
) -> tuple[bool, List[dict]]:
    """True khi mọi user V2 có total_day − top500 > exit_gap_min_vnd."""
    if not v2_usernames:
        return True, []
    if cms_by_name is None:
        cms_by_name = _cms_by_username(_fetch_all_cms_candidates())

    details: List[dict] = []
    for username in v2_usernames:
        u = str(username or "").strip()
        if not u:
            continue
        row = cms_by_name.get(u.lower())
        total_day = _to_int(row.get("total_day"), 0) if row else 0
        gap = total_day - money_500
        details.append({"username": u, "total_day": total_day, "gap": gap})
        if gap <= exit_gap_min_vnd:
            return False, details
    return True, details


def format_top_bet_pick_report(pick: TopBetPickResult, *, user_count: int = 8) -> str:
    lines: List[str] = []
    raw = pick.money_500
    moc = pick.compare_moc or effective_compare_moc(raw, pick.threshold_offset_vnd)
    off = pick.threshold_offset_vnd
    lines.append(f"📊 Mốc top {TARGET_TOP_IDX} (moneyBet) = {raw:,}")
    lines.append(
        f"   Mốc so sánh = top{TARGET_TOP_IDX} − {off:,} = {moc:,}"
    )
    lines.append(
        f"   Dưới top {TARGET_TOP_IDX} (tổng cược > {moc:,}): "
        f"{len(pick.duoi_top)} user CMS"
    )
    if pick.duoi_top:
        prev_b = ", ".join(
            f"{r['username']}({r['total_day']:,})" for r in pick.duoi_top[:15]
        )
        if len(pick.duoi_top) > 15:
            prev_b += f", … (+{len(pick.duoi_top) - 15})"
        lines.append(f"      {prev_b}")
    lines.append(
        f"   Trên top {TARGET_TOP_IDX} (tổng cược < {moc:,}): "
        f"{len(pick.tren_top)} user CMS"
    )
    if pick.tren_top:
        prev_a = ", ".join(
            f"{r['username']}({r['total_day']:,})" for r in pick.tren_top[:15]
        )
        lines.append(f"      {prev_a}")
    lines.append(f"   Quy tắc: {pick.rule}")
    lines.append(f"   Đã chọn {len(pick.selected)}/{user_count}:")
    for i, row in enumerate(pick.selected, 1):
        u = row["username"]
        reason = pick.pick_reasons.get(u, "")
        lines.append(
            f"      {i}. {u} — total_day={row['total_day']:,}"
            + (f" | {reason}" if reason else "")
        )
    return "\n".join(lines)


def _get_v2_target_count(default_count: int = 8) -> int:
    cfg = load_config() or {}
    v2_list = cfg.get("PRIORITY_USERS_V2", [])
    if isinstance(v2_list, list):
        normalized = [str(u or "").strip() for u in v2_list if str(u or "").strip()]
        if normalized:
            return len(normalized)
    return default_count


def _fetch_top_bet_daily_list(
    username: str, date: str | None = None, limit: int = 500
) -> Optional[List[dict]]:
    if not username:
        return None
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    url = "https://gameapi.tele68.com/v1/event/top-bet/daily"
    params = {"date": date, "limit": limit}
    resp = game_request_with_retry(
        username, "GET", url, params=params, extra_headers=_TOP_BET_HEADERS
    )
    if not resp:
        return None
    if resp.status_code == 400 and limit > 200:
        params["limit"] = 200
        resp = game_request_with_retry(
            username, "GET", url, params=params, extra_headers=_TOP_BET_HEADERS
        )
    if not resp or resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    return data


def _money_bet_at_top_idx(data: List[dict], top_idx: int = TARGET_TOP_IDX) -> int | None:
    for entry in data:
        if _to_int(entry.get("idx"), 0) == top_idx:
            amount = _to_int(entry.get("moneyBet"), 0)
            return amount if amount > 0 else None
    return None


def compute_top_bet_daily_gap_pick(
    username: str,
    *,
    date: str | None = None,
    limit: int = 500,
    user_count: int | None = None,
    max_entry_gap_vnd: int | None = None,
) -> TopBetPickResult:
    """Lấy mốc top 500, lọc gap, sort total_day, chọn user_count user."""
    empty = TopBetPickResult()
    if user_count is None:
        user_count = _get_v2_target_count(default_count=8)
    user_count = max(0, int(user_count))
    if user_count <= 0:
        empty.rule = "USER_COUNT=0"
        return empty

    data = _fetch_top_bet_daily_list(username, date=date, limit=limit)
    if not data:
        empty.rule = "không lấy được top-bet/daily"
        return empty

    money_500_raw = _money_bet_at_top_idx(data, TARGET_TOP_IDX)
    if not money_500_raw:
        empty.rule = f"không có mốc idx={TARGET_TOP_IDX}"
        return empty

    entry_gap = _threshold_offset_vnd() if max_entry_gap_vnd is None else max(0, int(max_entry_gap_vnd))
    candidates = _fetch_all_cms_candidates()
    return _pick_v2_users_by_gap_filter(
        candidates,
        money_500_raw=money_500_raw,
        max_entry_gap_vnd=entry_gap,
        user_count=user_count,
    )


def compute_top_bet_daily_v2_pick(
    username: str,
    *,
    date: str | None = None,
    limit: int = 500,
    user_count: int | None = None,
) -> TopBetPickResult:
    """Alias — dùng logic gap filter (scheduler / CLI)."""
    return compute_top_bet_daily_gap_pick(
        username, date=date, limit=limit, user_count=user_count
    )


def compute_top_bet_daily_v2_users(
    username: str,
    *,
    date: str | None = None,
    limit: int = 500,
    user_count: int | None = None,
) -> List[dict]:
    return compute_top_bet_daily_v2_pick(
        username, date=date, limit=limit, user_count=user_count
    ).selected


def compute_top_bet_daily_v2_usernames(
    username: str,
    *,
    date: str | None = None,
    limit: int = 500,
    user_count: int | None = None,
) -> List[str]:
    pick = compute_top_bet_daily_v2_pick(
        username, date=date, limit=limit, user_count=user_count
    )
    return [str(row["username"]).strip() for row in pick.selected if row.get("username")]


def fetch_top_bet_daily(username, date=None, limit=500, nearest_users_count=None):
    if nearest_users_count is None:
        nearest_users_count = _get_v2_target_count(default_count=8)

    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    data = _fetch_top_bet_daily_list(username, date=date, limit=limit)
    if not data:
        print("❌ Lỗi lấy top cược ngày (không có dữ liệu hoặc request thất bại)")
        return

    idx_lo = max(1, TARGET_TOP_IDX - 15)
    print(f"\nTOP cược ngày {date} (hạng {idx_lo}-{TARGET_TOP_IDX}):\n")
    print(f"{'Idx':>4} | {'Nickname':<20} | {'MoneyBet':>12} | {'Prize':>8}")
    print("-" * 55)
    printed_count = 0
    for entry in data:
        idx = entry.get("idx")
        try:
            idx_int = int(idx)
        except Exception:
            continue
        if idx_lo <= idx_int <= TARGET_TOP_IDX:
            nickname = entry.get("nickname", "")
            money_bet = entry.get("moneyBet", "0")
            prize = entry.get("prize", 0)
            print(f"{idx_int:>4} | {nickname:<20} | {int(float(money_bet)):,} | {prize:,}")
            printed_count += 1
    if printed_count == 0:
        print("⚠️ Không có dòng nào trong khoảng hạng yêu cầu.")

    money_500_raw = _money_bet_at_top_idx(data, TARGET_TOP_IDX)
    if not money_500_raw:
        print(f"\n⚠️ Không lấy được mốc idx={TARGET_TOP_IDX}.")
        return

    candidates = _fetch_all_cms_candidates()
    entry_gap = _threshold_offset_vnd()
    pick = _pick_v2_users_by_gap_filter(
        candidates,
        money_500_raw=money_500_raw,
        max_entry_gap_vnd=entry_gap,
        user_count=nearest_users_count,
    )
    print("\n" + format_top_bet_gap_pick_report(pick, user_count=nearest_users_count))
    closest = pick.selected
    if not closest:
        print("⚠️ Không có user được chọn.")
        return
    print(f"\n{'No':>3} | {'Username':<20} | {'TotalDay':>12}")
    print("-" * 40)
    for i, row in enumerate(closest, 1):
        print(f"{i:>3} | {row['username']:<20} | {row['total_day']:>12,}")


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        username = sys.argv[1]
    else:
        username = input("Nhập username: ").strip()
        if not username:
            print("Chưa nhập username!")
            sys.exit(1)
    fetch_top_bet_daily(username)
