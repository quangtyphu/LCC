import json
import asyncio
import requests
import time
import contextlib
from get_balance import get_balance as fetch_balance
from constants import allowed_events, active_ws
import constants  # dùng constants.session_seen (tránh global cục bộ)
from chiaTien_Acc import run_assigner, enqueue_bets
from auto_withdraw_on_won_session import handle_won_session_auto_withdraw
from jackpot_history_notifier import check_and_notify_jackpot
from bet_totals_increment import increment_bet_totals
import tai_xiu_skew_tracker as skew_tracker

API_BASE = "http://127.0.0.1:3000"  # URL server.js của bạn


async def _delayed_skew_finalize(session_id, attempt: int = 1) -> None:
    """Tối đa 10s sau phiên mới: chốt phiên trước nếu chưa có KQ (đã có KQ thì báo sớm hơn)."""
    if not session_id:
        return
    await asyncio.sleep(10)
    try:
        if skew_tracker.try_finalize(session_id):
            return
        if attempt == 1:
            skew_tracker.mark_needs_retry(session_id)
    except Exception as e:
        print(f"⚠️ [Tài/Xỉu lệch] finalize phiên {session_id}: {e}", flush=True)


# ------------------- Phiên trước để kiểm tra streak -------------------
prev_session_users = {}  # {session_id: [username1, username2,...]}

# ------------------- Cập nhật balance -------------------
def update_balance(user, balance, *, silent=False):
    if balance is None:
        return
    try:
        r = requests.put(f"{API_BASE}/api/users/{user}", json={"balance": balance}, timeout=3)
        if r.status_code != 200:
            print(f"⚠️ Lỗi update balance API: {r.text}", flush=True)
    except Exception as e:
        print(f"⚠️ Không kết nối được API users: {e}", flush=True)

# ------------------- Yêu cầu refresh balance qua WS -------------------
def request_balance_refresh(user, reason=""):
    entry = active_ws.get(user)
    if not entry:
        return
    entry["poke_balance"] = True
    if reason:
        print(f"🔎 [{user}] {reason} -> yêu cầu your-info để cập nhật balance", flush=True)
    else:
        print(f"🔎 [{user}] Yêu cầu your-info để cập nhật balance", flush=True)


async def refresh_balance_via_api(user, reason=""):
    if reason:
        print(f"🔎 [{user}] {reason} -> gọi get_balance API", flush=True)
    else:
        print(f"🔎 [{user}] Gọi get_balance API", flush=True)
    try:
        result = await asyncio.to_thread(lambda: fetch_balance(user))
    except Exception as e:
        print(f"⚠️ [{user}] get_balance lỗi: {e}", flush=True)
        result = {"ok": False}
    if not result or not result.get("ok"):
        # fallback: poke WS your-info
        request_balance_refresh(user, "get_balance thất bại")
        return
    balance = result.get("balance")
    print(f"✅ [{user}] get_balance OK: {balance}", flush=True)


async def _check_bet_success_later(user, session_id, delay_sec=30):
    await asyncio.sleep(delay_sec)
    entry = active_ws.get(user)
    if not entry:
        return
    pending = entry.get("pending_bet")
    if not pending or pending.get("session_id") != session_id:
        return
    if pending.get("resolved"):
        return
    pending["resolved"] = True
    asyncio.create_task(refresh_balance_via_api(user, "Không thấy bet-result"))

# ------------------- Cập nhật streak -------------------
def update_streak(username, result):
    """
    result: 'won' hoặc 'lost'
    """
    # normalize username -> string
    if isinstance(username, str):
        uname = username
    elif isinstance(username, (list, tuple)) and username:
        uname = str(username[0])
    elif isinstance(username, dict):
        uname = str(username.get("username") or username.get("user") or json.dumps(username))
    else:
        uname = str(username)

    try:
        r = requests.post(f"{API_BASE}/streaks/update", json={
            "username": uname,
            "result": result
        }, timeout=3)
        # Bỏ log thành công; chỉ in lỗi khi API trả lỗi
        if r.status_code != 200:
            print(f"⚠️ Lỗi update streak: {r.text}", flush=True)
    except Exception as e:
        print(f"⚠️ Không kết nối được API streaks: {e}", flush=True)
# ------------------- Xử lý sự kiện -------------------
async def handle_event(user, msg):
    try:
        arr = json.loads(msg[len("42/tx,"):])
    except Exception as e:
        print(f"⚠️ [{user}] Lỗi parse JSON: {e} | raw={msg}", flush=True)
        return

    if not isinstance(arr, list) or not arr:
        print(f"⚠️ [{user}] Gói tin không hợp lệ: {arr}", flush=True)
        return

    event, data, *rest = (arr + [None, {}])[:3]
    if not isinstance(data, dict):
        data = {}

    # ------------------- Thông tin user -------------------
    if event == "your-info":
        balance = data.get("money") or data.get("balance") or 0
        try:
            await asyncio.to_thread(
                lambda: requests.put(f"{API_BASE}/api/users/{user}", json={"balance": int(balance)}, timeout=5)
            )
        except Exception:
            pass

        # Debounce: tránh fetch lặp trong 30s
        entry = active_ws.get(user) or {}
        now = time.time()
        last_fetch = entry.get("last_fetch_at", 0)
        if now - last_fetch < 30:
            return
        entry["last_fetch_at"] = now

        async def fetch_bg():
            try:
                from fetch_transactions import fetch_transactions_async
                await fetch_transactions_async(user, "DEPOSIT", 10)
                await fetch_transactions_async(user, "WITHDRAW", 10)
            except Exception as e:
                print(f"⚠️ [{user}] Lỗi fetch tx: {e}", flush=True)

        asyncio.create_task(fetch_bg())
        return

    # ------------------- Các event khác -------------------
    if event in allowed_events:

        # new-session: chỉ cho phép 1 acc đầu tiên xử lý
        if event == "new-session":
            session_id = data.get("id")
            if constants.session_seen == session_id:
                return
            constants.session_seen = session_id
            session_started_at = time.time()
            print(f"🆕 [{user}] xử lý phiên {session_id}", flush=True)

            prev_sid_for_skew = getattr(constants, "last_session_id", None)

            # --- Kiểm tra phiên trước để update lost nếu chưa nhận win ---
            if hasattr(constants, "last_session_id") and constants.last_session_id in prev_session_users:
                previous_users = prev_session_users.pop(constants.last_session_id)
                # normalize -> danh sách username (chuỗi)
                normalized = []
                for item in previous_users:
                    if isinstance(item, (list, tuple)) and item:
                        normalized.append(str(item[0]))
                    elif isinstance(item, dict):
                        normalized.append(str(item.get("username") or item.get("user") or json.dumps(item)))
                    else:
                        normalized.append(str(item))

                async def delayed_lost_check(users_list):
                    await asyncio.sleep(10)
                    for uname in users_list:
                        try:
                            update_streak(uname, "lost")
                        except Exception as e:
                            print(f"⚠️ Failed update_streak for {uname}: {e}", flush=True)

                asyncio.create_task(delayed_lost_check(normalized))

            constants.last_session_id = session_id

            online_users = list(active_ws.keys())
            final_bets = run_assigner(online_users)

            try:
                from constants import load_config
                from jackpot_night_extend import consume_post_jackpot_round

                consume_post_jackpot_round(session_id, load_config())
            except Exception as _gr:
                print(f"⚠️ [{user}] post_jackpot grace: {_gr}", flush=True)

            if final_bets:

                # --- Refactor: Mỗi user có 1 assign_task riêng ---
                for u, amount, door, delay in final_bets:
                    entry_u = active_ws.get(u)
                    if entry_u:
                        old = entry_u.pop("assign_task", None)
                        if old and not old.done():
                            old.cancel()
                            try:
                                await old
                            except Exception:
                                pass
                        # Tạo task enqueue_bets chỉ cho user này
                        entry_u["assign_task"] = asyncio.create_task(enqueue_bets([(u, amount, door, delay)]))
                        # Gắn pending bet để check nếu không có bet-result
                        entry_u["pending_bet"] = {
                            "session_id": session_id,
                            "assigned_at": time.time(),
                            "amount": amount,
                            "door": door,
                            "resolved": False,
                        }
                        old_check = entry_u.pop("pending_bet_task", None)
                        if old_check and not old_check.done():
                            old_check.cancel()
                            with contextlib.suppress(Exception):
                                await old_check
                        delay_sec = max(0, 30 - (time.time() - session_started_at))
                        entry_u["pending_bet_task"] = asyncio.create_task(
                            _check_bet_success_later(u, session_id, delay_sec=delay_sec)
                        )

            # --- Lưu tổng cược theo phiên (dùng cho thông báo nổ hũ) ---
            try:
                total_tai = sum(amt for (_, amt, door, _) in final_bets if str(door).upper() == "TAI")
                total_xiu = sum(amt for (_, amt, door, _) in final_bets if str(door).upper() == "XIU")
                total_any = max(total_tai, total_xiu)
                constants.session_bet_totals[session_id] = {
                    "tai": int(total_tai),
                    "xiu": int(total_xiu),
                    "total": int(total_any),
                }
                skew_tracker.register_session(session_id, total_tai, total_xiu)
                if len(constants.session_bet_totals) > 500:
                    constants.session_bet_totals.clear()
            except Exception:
                pass

            # --- Lưu user cược của phiên hiện tại ---
            # lưu CHỈ username (chuỗi) để dễ xoá khi có win
            prev_session_users[session_id] = [str(u) for u, *_ in final_bets]

            if prev_sid_for_skew:
                asyncio.create_task(_delayed_skew_finalize(prev_sid_for_skew, 1))
            for retry_sid in skew_tracker.pop_retry_sessions():
                asyncio.create_task(_delayed_skew_finalize(retry_sid, 2))


        elif event == "bet-result":
            amount = data.get("amount")
            bet_type = data.get("type", "").upper()
            bet_label = "Tài" if bet_type == "TAI" else "Xỉu"
            post_balance = data.get("postBalance")

            update_balance(user, post_balance, silent=True)
            entry_u = active_ws.get(user)
            if entry_u and entry_u.get("pending_bet"):
                entry_u["pending_bet"]["resolved"] = True

            if post_balance is not None:
                print(
                    f"✅ [{user.ljust(15)}] "
                    f"Đặt cược {bet_label.ljust(4)} "
                    f"- {str(amount).rjust(8)} "
                    f"| Số dư mới = {str(post_balance).rjust(10)}",
                    flush=True
                )
            else:
                print(
                    f"✅ [{user.ljust(15)}] "
                    f"Đặt cược {bet_label.ljust(4)} "
                    f"- {str(amount).rjust(8)}",
                    flush=True
                )

            increment_bet_totals(user, amount)

        elif event == "bet_refund":
            amount = data.get("amount")
            bet_type = data.get("type", "").upper()
            bet_label = "Tài" if bet_type == "TAI" else "Xỉu"
            post_balance = data.get("postBalance") or data.get("balance")

            update_balance(user, post_balance, silent=True)
            entry_u = active_ws.get(user)
            matched_pending = False
            if entry_u and entry_u.get("pending_bet"):
                pending = entry_u["pending_bet"]
                pending_door = str(pending.get("door") or "").upper()
                pending_amount = pending.get("amount")
                if pending_door == bet_type and (pending_amount is None or pending_amount == amount):
                    pending["resolved"] = True
                    matched_pending = True
                else:
                    print(
                        f"⚠️ [{user}] Bet refund không khớp pending "
                        f"(pending={pending_door} {pending_amount}, refund={bet_type} {amount})",
                        flush=True
                    )

            print(
                f"⚠️ [{user.ljust(15)}] "
                f"Bet refund {bet_label.ljust(4)} "
                f"- {str(amount).rjust(8)} "
                f"| Balance={str(post_balance).rjust(10)}",
                flush=True
            )

            # Chỉ refresh nếu thiếu post_balance và refund khớp pending
            if post_balance is None and matched_pending:
                asyncio.create_task(refresh_balance_via_api(user, "Bet refund"))

        elif event == "session-result":
            sid = data.get("id") or data.get("sessionId") or data.get("session_id")
            dices = data.get("dices") or data.get("dice")
            skew_tracker.note_session_winner(sid, dices, source="session-result")

        elif event == "won-session":
            balance = data.get("balance")
            prize = data.get("prize", 0)
            dices = data.get("dices", [])
            update_balance(user, balance, silent=True)
            print(f"🎲 [{user}] Thắng phiên | Dices={dices} | Prize={prize} | Balance={balance}", flush=True)
            sid_skew = (
                data.get("sessionId")
                or data.get("session_id")
                or data.get("id")
                or getattr(constants, "last_session_id", None)
            )
            skew_tracker.note_session_winner(sid_skew, dices, source="won-session")
            # Fallback: check 111/666 on won-session, then notify jackpot
            try:
                dices_str = "".join(str(int(x)) for x in dices) if isinstance(dices, (list, tuple)) else ""
                if dices_str in ("111", "666"):
                    try:
                        from jackpot_night_extend import cancel_extend_on_jackpot_hit

                        cancel_extend_on_jackpot_hit()
                    except Exception as _ex:
                        print(f"⚠️ [{user}] jackpot_night_extend cancel: {_ex}", flush=True)
                    session_payload = dict(data) if isinstance(data, dict) else {}
                    if "session_id" not in session_payload and "sessionId" not in session_payload:
                        if hasattr(constants, "last_session_id"):
                            session_payload["session_id"] = constants.last_session_id
                    asyncio.create_task(asyncio.to_thread(check_and_notify_jackpot, user, session_payload))
            except Exception as e:
                print(f"⚠️ [{user}] Lỗi xử lý won-session jackpot: {e}", flush=True)
            # --- Xoá user khỏi list prev_session_users để không bị delayed lost ---
            if hasattr(constants, "last_session_id") and constants.last_session_id in prev_session_users:
                if user in prev_session_users[constants.last_session_id]:
                    prev_session_users[constants.last_session_id].remove(user)
            update_streak(user, "won")
            
            # AUTO WITHDRAW khi won-session
            try:
                sid = None
                if isinstance(data, dict):
                    sid = data.get("sessionId") or data.get("session_id")
                if sid is None:
                    sid = getattr(constants, "last_session_id", None)
                handle_won_session_auto_withdraw(user, balance, session_id=sid)
            except Exception as e:
                print(f"❌ [{user}] Lỗi auto withdraw: {e}")
                import traceback
                traceback.print_exc()
