# check_jackpot_session.py
"""
Tra cứu phiên Tài Xỉu (session-summary), tính chia hũ và ghi DB.

- Tổng cược toàn game (một bên Tài/Xỉu): overall.totalAmount/2 hoặc betSummaries[0].totalAmount
- Số tiền hũ: API jackpot-history (hoặc truyền --jackpot)
- Tổng cược của mình: --my-bet (bắt buộc; CMS không còn bet_history)

Công thức: số_tiền_nhận = (tổng_cược_mình * số_tiền_hũ) / tổng_cược_toàn_game

Mẫu:
  python check_jackpot_session.py traisoi66 6730130 --my-bet 30000
  python check_jackpot_session.py traisoi66 6730130 --my-bet 30000 --dry-run
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from typing import Any

from game_api_helper import game_request_with_retry
from jackpot_history_notifier import _fetch_jackpot_history, _find_detail_by_session
from jackpot_session_db import DB_PATH, upsert_jackpot_record

SESSION_SUMMARY_URL = "https://wtx.tele68.com/v1/tx/session-summary"


def _game_total_one_side(overall: dict[str, Any] | None) -> float | None:
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


def fetch_session_summary(username: str, session_id: int | str) -> dict[str, Any] | None:
    resp = game_request_with_retry(
        username,
        "GET",
        SESSION_SUMMARY_URL,
        params={"sessionId": session_id},
    )
    if not resp or resp.status_code != 200:
        print(
            f"❌ session-summary HTTP {resp.status_code if resp else 'none'}",
            flush=True,
        )
        return None
    try:
        data = resp.json()
    except Exception as e:
        print(f"❌ Parse session-summary: {e}", flush=True)
        return None
    return data if isinstance(data, dict) else None


def fetch_jackpot_amount(username: str, session_id: int | str) -> float | None:
    details = _fetch_jackpot_history(username, limit=30)
    if not details:
        return None
    detail = _find_detail_by_session(details, session_id)
    if not isinstance(detail, dict):
        return None
    raw = detail.get("jackpotAmount") or detail.get("jackpot_amount")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def run(
    username: str,
    session_id: int,
    my_bet: float | None,
    jackpot_override: float | None,
    dry_run: bool,
) -> int:
    summary = fetch_session_summary(username, session_id)
    if not summary:
        return 1
    if summary.get("id") is not None and int(summary["id"]) != int(session_id):
        print(f"⚠️ id trong response ({summary.get('id')}) khác session_id đã hỏi", flush=True)

    overall = summary.get("overall")
    game_total = _game_total_one_side(overall)
    if game_total is None or game_total <= 0:
        print("❌ Không lấy được tổng cược một bên (game) từ overall", flush=True)
        return 1

    jackpot_side = summary.get("resultTruyenThong")
    ts_raw = summary.get("timestamp")

    if my_bet is None:
        print("❌ Thiếu --my-bet (tổng cược của bạn ở phiên đó).", flush=True)
        return 1

    jackpot_amount = jackpot_override
    if jackpot_amount is None:
        jackpot_amount = fetch_jackpot_amount(username, session_id)
    if jackpot_amount is None:
        print("❌ Không lấy được số tiền hũ (thử --jackpot hoặc kiểm tra jackpot-history)", flush=True)
        return 1

    if game_total <= 0:
        print("❌ Tổng cược toàn game không hợp lệ", flush=True)
        return 1

    amount_received = (my_bet * jackpot_amount) / game_total

    print(f"\nPhiên: {session_id}", flush=True)
    print(f"Nổ hũ (cửa): {jackpot_side}", flush=True)
    print(f"Số tiền hũ: {jackpot_amount:,.2f}", flush=True)
    print(f"Tổng cược của mình: {my_bet:,.2f}", flush=True)
    print(f"Tổng cược toàn game (một bên): {game_total:,.2f}", flush=True)
    print(f"Số tiền nhận (chia hũ): {amount_received:,.2f}", flush=True)
    if ts_raw:
        print(f"Thời gian phiên (API): {ts_raw}", flush=True)

    if dry_run:
        print("\n(dry-run, không ghi DB)", flush=True)
        return 0

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

    try:
        upsert_jackpot_record(
            int(session_id),
            username,
            my_bet,
            game_total,
            jackpot_amount,
            amount_received,
            jackpot_side if isinstance(jackpot_side, str) else None,
            ts_raw if isinstance(ts_raw, str) else None,
            api_username=username,
            dices=dices,
            dice_point=dice_point,
            overall_total_amount=overall_total_f,
        )
        print(f"\n✅ Đã cập nhật jackpot_session_records trong {DB_PATH}", flush=True)
    except sqlite3.Error as e:
        print(f"❌ Ghi DB lỗi: {e}", flush=True)
        return 1
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Check phiên nổ hũ + ghi DB")
    p.add_argument("username", help="Username trong CMS (lấy JWT/proxy)")
    p.add_argument("session_id", type=int, help="ID phiên (vd: 6730130)")
    p.add_argument("--my-bet", type=float, default=None, help="Tổng cược của bạn ở phiên đó")
    p.add_argument("--jackpot", type=float, default=None, help="Số tiền hũ (bỏ qua jackpot-history)")
    p.add_argument("--dry-run", action="store_true", help="Chỉ in, không ghi SQLite")
    args = p.parse_args()

    code = run(
        args.username,
        args.session_id,
        args.my_bet,
        args.jackpot,
        args.dry_run,
    )
    sys.exit(code)


if __name__ == "__main__":
    main()
