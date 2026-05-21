"""
tan_thu_vmission_service.py

Mục tiêu:
1) Mode `check`: lấy trạng thái tân thủ (mốc level 1-2-3) từ API `tan-thu-vmission`
   - `status`: "ready" (chưa nhận), "claimed" (đã nhận), các trạng thái khác (chưa sẵn sàng)
   - `isWon`: mốc đó đã đạt hay chưa
2) Mode `claim`: claim theo thứ tự 1 -> 2 -> 3
   - Chỉ claim khi trước đó đã `claimed` và level hiện tại `ready` + `isWon=true`
   - Mặc định chỉ claim các level từng là `ready` tại lần `check` gần nhất (tránh claim nhầm ngoài ý muốn)

Gợi ý chạy:
  python tan_thu_vmission_service.py check --all
  python tan_thu_vmission_service.py check --user <username>
  python tan_thu_vmission_service.py claim --all
  python tan_thu_vmission_service.py claim --user <username>
  python tan_thu_vmission_service.py claim --all --ignore-last-check
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Fix encoding cho Windows console
if sys.platform == "win32":
    os.system("chcp 65001 > nul")

from game_api_helper import game_request_with_retry, update_user_balance
from get_active_accounts import get_active_accounts, get_all_userprofiles


TAN_THU_LIST_URL = "https://wlb.tele68.com/v1/mission/tan-thu-vmission"
LEVELS = [1, 2, 3]

# Dùng để ghi state giữa `check` và `claim`
STATE_FILE = Path(__file__).with_name("tan_thu_vmission_state.json")

# Một số endpoint nhạy origin/referer; dùng tương tự request browser bạn từng đưa.
EXTRA_HEADERS = {
    "origin": "https://lc79b.bet",
    "referer": "https://lc79b.bet/",
    "accept-language": "vi-VN,vi;q=0.9,fr-FR;q=0.8,fr;q=0.7,en-US;q=0.6,en;q=0.5",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    ),
}

CMS_API_BASE = os.environ.get("CMS_API_BASE", "http://127.0.0.1:3000")


def _cms_stored_onboarding_task(username: str) -> int | None:
    """Đọc onboarding_task từ CMS. Chỉ trả 1/2/3; 0 hoặc thiếu → None (vẫn gọi API tân thủ trừ khi gate = 3)."""
    try:
        r = requests.get(f"{CMS_API_BASE}/api/vip-x10-params/{username}", timeout=6)
        if r.status_code != 200:
            return None
        payload = r.json()
        row = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(row, dict):
            return None
        v = _to_int(row.get("onboarding_task"), None)
        if v in (1, 2, 3):
            return v
        return None
    except Exception:
        return None


def _tan_thu_skip_game_api_by_cms(username: str) -> bool:
    """CMS đã lưu onboarding_task=3 → không gọi API game tan-thu-vmission nữa."""
    return _cms_stored_onboarding_task(username) == 3


def _to_int(val: Any, default: int | None = None) -> int | None:
    try:
        return int(float(val))
    except Exception:
        return default


def _norm_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        s = val.strip().lower()
        return s in ("true", "1", "yes", "y")
    return False


def _entry_id(entry: dict[str, Any]) -> str | None:
    # API thường trả cả `id` lẫn `_id` (trùng nhau).
    _id = entry.get("_id")
    if _id:
        return str(_id)
    _id2 = entry.get("id")
    if _id2:
        return str(_id2)
    return None


def fetch_tan_thu_missions(
    username: str, *, bypass_cms_gate: bool = False
) -> list[dict[str, Any]] | None:
    """
    GET tan-thu-vmission qua game API.
    Trước đó đọc CMS: nếu onboarding_task đã lưu = 3 thì không gọi game (trả []).
    Khi đang claim cần refresh sau từng bước → truyền bypass_cms_gate=True.
    """
    if not bypass_cms_gate and _tan_thu_skip_game_api_by_cms(username):
        return []
    resp = game_request_with_retry(
        username,
        "GET",
        TAN_THU_LIST_URL,
        extra_headers=EXTRA_HEADERS,
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


def update_onboarding_task_highest_claimed(username: str, highest_level: int) -> None:
    """
    server.js có endpoint:
      PUT /api/vip-x10-params/:username
    và field cần cập nhật là `onboarding_task` (1/2/3).

    Body chỉ gửi {"onboarding_task": ...}. Có dòng thì UPDATE; chưa có (GET 404) vẫn PUT
    để upsert (CMS: INSERT với x10_next mặc định 0 nếu client không gửi mốc x10).
    """
    if highest_level not in (1, 2, 3):
        return
    try:
        url = f"{CMS_API_BASE}/api/vip-x10-params/{username}"
        chk = requests.get(url, timeout=6)
        if chk.status_code == 200:
            try:
                payload = chk.json()
                row = payload.get("data") if isinstance(payload, dict) else None
                cur = _to_int(row.get("onboarding_task"), None) if isinstance(row, dict) else None
                if cur is not None and cur >= highest_level:
                    return
            except Exception:
                pass
        elif chk.status_code not in (404, 200):
            print(
                f"⚠️ [{username}] Không đọc được vip-x10-params (HTTP {chk.status_code}): {chk.text[:120]}",
                flush=True,
            )
            return

        r = requests.put(url, json={"onboarding_task": highest_level}, timeout=8)
        if not r.ok:
            print(
                f"⚠️ [{username}] Lỗi cập nhật onboarding_task={highest_level} (HTTP {r.status_code}): {r.text[:120]}",
                flush=True,
            )
            return
    except Exception as e:
        print(f"⚠️ [{username}] Không update onboarding_task được: {e}", flush=True)


def highest_claimed_tan_thu_level(levels_map: dict[str, dict[str, Any]]) -> int:
    """Mốc tân thủ cao nhất đã nhận thưởng (chỉ cần status=claimed; không phụ thuộc isWon)."""
    best = 0
    for lv in (1, 2, 3):
        d = levels_map.get(str(lv), {})
        if str(d.get("status") or "").strip().lower() == "claimed":
            best = max(best, lv)
    return best


def sync_onboarding_task_from_levels_map(username: str, levels_map: dict[str, dict[str, Any]]) -> None:
    h = highest_claimed_tan_thu_level(levels_map)
    if h:
        update_onboarding_task_highest_claimed(username, h)


def build_levels_map(missions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    levels_map: dict[str, dict[str, Any]] = {}
    for m in missions:
        lvl = _to_int(m.get("level"), None)
        if lvl not in LEVELS:
            continue
        lvl_s = str(lvl)
        levels_map[lvl_s] = {
            "id": _entry_id(m),
            "isWon": _norm_bool(m.get("isWon")),
            "status": (m.get("status") or "").strip(),
            "bonusAmount": m.get("bonusAmount"),
        }
    return levels_map


def get_usernames_from_args(args: argparse.Namespace) -> list[str]:
    if args.user:
        return [args.user]

    if args.all:
        profiles = get_all_userprofiles()
        usernames = [str(p.get("username") or "").strip() for p in profiles]
        return [u for u in usernames if u]

    # Default: lấy user đang active (status="Đang Chơi")
    profiles = get_active_accounts()
    usernames = [str(p.get("username") or "").strip() for p in profiles]
    return [u for u in usernames if u]


def check_user(username: str) -> dict[str, Any] | None:
    missions = fetch_tan_thu_missions(username)
    if missions is None:
        print(f"❌ [{username}] Không lấy được missions tân thủ", flush=True)
        return None
    if missions == []:
        return {"skipped": True, "reason": "cms_onboarding_task_3"}

    levels_map = build_levels_map(missions)

    # Đồng bộ onboarding_task theo trạng thái hiện tại (kể cả đã nhận từ trước, không cần vừa claim).
    sync_onboarding_task_from_levels_map(username, levels_map)

    return {"levels": levels_map}


def save_state(state: dict[str, Any]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def load_state() -> dict[str, Any] | None:
    if not STATE_FILE.exists():
        return None
    try:
        raw = STATE_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def claim_user(username: str, restrict_to_last_check: bool) -> None:
    state = load_state() if restrict_to_last_check else None
    recorded_levels: dict[str, dict[str, Any]] = {}
    if state:
        recorded_levels = (
            (state.get("users", {}) or {}).get(username, {}) or {}
        ).get("levels", {}) or {}

    # Luôn fetch lại current để đảm bảo thứ tự / trạng thái hiện tại
    missions = fetch_tan_thu_missions(username)
    if missions is None:
        print(f"❌ [{username}] Không lấy được missions để claim", flush=True)
        return
    if missions == []:
        return

    cur_levels = build_levels_map(missions)

    def prev_claimed(upto_level: int) -> bool:
        # Yêu cầu các level < upto_level phải có status == "claimed"
        for lv in range(1, upto_level):
            d = cur_levels.get(str(lv), {})
            if (d.get("status") or "") != "claimed":
                return False
        return True

    def recorded_ready_won(level: int) -> bool:
        d = recorded_levels.get(str(level), {}) or {}
        return _norm_bool(d.get("isWon")) and (d.get("status") or "") == "ready"

    for lv in LEVELS:
        d = cur_levels.get(str(lv), {})
        if not d or not d.get("id"):
            break

        # Kiểm tra thứ tự: level trước phải đã claimed
        if lv > 1 and not prev_claimed(lv):
            break

        # Nếu level đã claimed thì bỏ qua, vẫn cho phép tiếp level kế.
        if (d.get("status") or "") == "claimed":
            continue

        # Restrict: chỉ claim level từng ready tại lần check gần nhất
        if restrict_to_last_check and not recorded_ready_won(lv):
            break

        if not _norm_bool(d.get("isWon")):
            break

        if (d.get("status") or "") != "ready":
            break

        claim_id = str(d.get("id"))
        claim_resp = game_request_with_retry(
            username,
            "PUT",
            TAN_THU_LIST_URL,
            json_data={"id": claim_id},
            extra_headers=EXTRA_HEADERS,
        )

        if not claim_resp or claim_resp.status_code not in (200, 201):
            break

        # Response mẫu: {"balance": 71359}
        try:
            payload = claim_resp.json()
        except Exception:
            payload = {}

        new_balance = payload.get("balance")
        if new_balance is not None:
            try:
                update_user_balance(username, float(new_balance))
            except Exception:
                pass

        # Refresh lại trạng thái cho đúng thứ tự
        time.sleep(1.5)
        missions2 = fetch_tan_thu_missions(username, bypass_cms_gate=True) or []
        cur_levels = build_levels_map(missions2)
        time.sleep(0.5)

    # Sau cùng: lưu mốc tân thủ cao nhất đã nhận (status=claimed) → onboarding_task 1/2/3.
    missions_end = fetch_tan_thu_missions(username, bypass_cms_gate=True) or []
    levels_end = build_levels_map(missions_end)
    sync_onboarding_task_from_levels_map(username, levels_end)

def main() -> None:
    # Nếu không truyền tham số → hỏi username rồi chỉ check 1 user (mode đơn giản).
    if len(sys.argv) == 1:
        username = input("Nhập username để kiểm tra tân thủ: ").strip()
        if not username:
            print("❌ Username không được để trống", flush=True)
            return
        print(f"\n[1/1] Checking {username} ...", flush=True)
        check_user(username)

        # Theo yêu cầu: nếu có mốc ready (isWon=true) thì tự claim theo thứ tự 1->2->3.
        # Không restrict theo lần check trước để tránh lệch trạng thái.
        print(f"\n[1/1] Claiming ready missions for {username} ...", flush=True)
        claim_user(username, restrict_to_last_check=False)
        return

    parser = argparse.ArgumentParser(description="Kiểm tra & claim nhiệm vụ tân thủ (tan-thu-vmission).")
    sub = parser.add_subparsers(dest="cmd")

    p_check = sub.add_parser("check", help="Check trạng thái level 1-2-3.")
    p_check.add_argument("--user", type=str, default=None, help="Username cần check.")
    p_check.add_argument("--all", action="store_true", help="Lấy tất cả user trong DB (không chỉ active).")
    p_check.add_argument("--sleep", type=float, default=1.0, help="Delay giữa các user.")

    p_claim = sub.add_parser("claim", help="Claim theo thứ tự 1 -> 2 -> 3.")
    p_claim.add_argument("--user", type=str, default=None, help="Username cần claim.")
    p_claim.add_argument("--all", action="store_true", help="Claim cho tất cả user active (hoặc --user).")
    p_claim.add_argument(
        "--ignore-last-check",
        action="store_true",
        help="Không dùng danh sách restrict từ lần check gần nhất.",
    )
    p_claim.add_argument("--sleep", type=float, default=2.0, help="Delay giữa các user.")

    args = parser.parse_args()

    if not args.cmd:
        parser.print_help()
        return
    usernames = get_usernames_from_args(args)
    if not usernames:
        print("Không có username nào để chạy.", flush=True)
        return

    if args.cmd == "check":
        state: dict[str, Any] = {
            "checkedAt": datetime.now(timezone.utc).isoformat(),
            "users": {},
        }
        for i, u in enumerate(usernames, 1):
            print(f"\n[{i}/{len(usernames)}] Checking {u} ...", flush=True)
            res = check_user(u)
            if res:
                state["users"][u] = res
            if i < len(usernames):
                time.sleep(max(0, float(args.sleep)))

        save_state(state)
        print(f"\n✅ Đã check xong. State lưu tại: {STATE_FILE}", flush=True)
        return

    if args.cmd == "claim":
        restrict = not bool(args.ignore_last_check)
        for i, u in enumerate(usernames, 1):
            print(f"\n[{i}/{len(usernames)}] Claiming {u} ...", flush=True)
            claim_user(u, restrict_to_last_check=restrict)
            if i < len(usernames):
                time.sleep(max(0, float(args.sleep)))
        print("\n✅ Claim xong.", flush=True)
        return


if __name__ == "__main__":
    main()

