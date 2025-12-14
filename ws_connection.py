# ws_connection.py  (CẬP NHẬT)
import asyncio
import json
import time
import socks
import websockets
import requests
import contextlib
import sys

from constants import WS_URL, active_ws
from token_utils import test_token
from jwt_manager import refresh_jwt
from ws_events import handle_event  # import xử lý event
from game_login import get_access_token, update_access_token_to_db

API_BASE = "http://127.0.0.1:3000"  # đổi thành URL server.js của bạn


# Dùng selector loop trên Windows để hỗ trợ socks.socksocket
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


# ------------------- HỖ TRỢ: chạy requests blocking trong thread để không block event loop ----------
async def _requests_put(path, json_data, timeout=5):
    return await asyncio.to_thread(lambda: requests.put(f"{API_BASE}{path}", json=json_data, timeout=timeout))



# ------------------- Cập nhật trạng thái user qua API (async) -------------------
async def update_user_status(user, status):
    try:
        # gọi trong thread để tránh block
        resp = await _requests_put(f"/api/users/{user}", {"status": status}, timeout=3)
        if resp.status_code == 200:
            print(f"💾 [{user}] Cập nhật trạng thái = {status}")
        else:
            print(f"⚠️ [{user}] Lỗi cập nhật status API: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"⚠️ [{user}] Không kết nối được API khi update status: {e}")


# ------------------- Gửi lệnh từ queue ra WS -------------------
async def _drain_outgoing_queue(ws, queue: asyncio.Queue, user: str):
    try:
        while True:
            payload = queue.get_nowait()
            if not isinstance(payload, tuple) or len(payload) != 2:
                print(f"⚠️ [{user}] payload lạ trong queue: {payload}")
                continue
            event, data = payload
            if event == "bet":
                to_send = "42/tx," + json.dumps(["bet", data], ensure_ascii=False)
                await ws.send(to_send)
            else:
                print(f"ℹ️ [{user}] Bỏ qua payload event={event}")
            queue.task_done()
    except asyncio.QueueEmpty:
        pass


# ------------------- Giữ WS cho 1 account (đơn-kết-nối theo conn_id) -------------------
async def handle_ws(acc, conn_id: str):
    """
    acc: dict chứa keys: username, proxy, jwt, ...
    conn_id: id kết nối hiện tại (được đặt khi tạo active_ws[user])
    """
    user = acc["username"]

    # Lấy entry & xác nhận conn_id còn hợp lệ trước khi chạy
    entry = active_ws.get(user)
    if not entry or entry.get("conn_id") != conn_id:
        print(f"⛔ [{user}] handle_ws start bị hủy (conn_id mismatch).")
        return

    # đảm bảo entry có lock & queue
    if "lock" not in entry:
        entry["lock"] = asyncio.Lock()
    queue: asyncio.Queue = entry.get("queue")
    if queue is None:
        queue = asyncio.Queue()
        entry["queue"] = queue

    try:
        proxy_str = acc.get("proxy")
        jwt = acc.get("jwt")

        # ===== 1) Proxy check trước với retry backoff =====
        try:
            host, port, puser, ppass = proxy_str.split(":")
            port = int(port)
        except Exception:
            print(f"🔐 [{user}] Đã Kết Nối Proxy ( Proxy Lỗi )")
            await update_user_status(user, "Proxy Lỗi")
            return

        backoffs = [0, 15, 30, 60, 120]  # nhanh hơn, vẫn 5 lần
        proxy_ok = False
        for attempt, delay in enumerate(backoffs, start=1):
            if delay:
                await asyncio.sleep(delay)
            test_sock = socks.socksocket()
            test_sock.set_proxy(socks.SOCKS5, host, port, True, puser, ppass)
            test_sock.setblocking(True)
            try:
                test_sock.connect(("wtx.tele68.com", 443))
                proxy_ok = True
                print(f"🔐 [{user}] Đã Kết Nối Proxy (attempt {attempt})")
                break
            except Exception:
                print(f"🔐 [{user}] Proxy lỗi (attempt {attempt})")
            finally:
                with contextlib.suppress(Exception):
                    test_sock.close()

        if not proxy_ok:
            await update_user_status(user, "Proxy Lỗi")
            return

        # socket dành cho websockets (không connect thử lại nữa)
        sock = socks.socksocket()
        sock.set_proxy(socks.SOCKS5, host, port, True, puser, ppass)
        sock.setblocking(False)
        # bỏ sock.settimeout(...) nếu có
        try:
            # kết nối thử tới host thật để kiểm tra proxy
            sock.connect(("wtx.tele68.com", 443))
            # print(f"🔐 [{user}] Đã Kết Nối Proxy")
        except Exception:
            print(f"🔐 [{user}] Đã Kết Nối Proxy ( Proxy Lỗi )")
            await update_user_status(user, "Proxy Lỗi")
            return

        # ===== 2) Token check & auto-refresh nếu lỗi =====
        jwt = acc.get("jwt")
        
        # Test token (timeout 3s)
        try:
            ok = await asyncio.wait_for(test_token(jwt, proxy_str), timeout=3)
        except Exception:
            ok = False
        
        if not ok:
            print(f"⚠️ [{user}] JWT lỗi → refresh tự động")
            
            # Refresh JWT mới (tự động xử lý accessToken nếu cần)
            try:
                new_jwt = await asyncio.to_thread(lambda: refresh_jwt(user))
                if new_jwt:
                    jwt = new_jwt
                    acc["jwt"] = jwt
                    await _requests_put(f"/api/users/{user}", {"jwt": jwt}, timeout=5)
                    print(f"🔑 [{user}] Đã refresh JWT mới")
                else:
                    print(f"❌ [{user}] Không refresh được JWT")
                    await update_user_status(user, "Token Lỗi")
                    return
            except Exception as e:
                print(f"❌ [{user}] Lỗi refresh JWT: {e}")
                await update_user_status(user, "Token Lỗi")
                return

        # JWT OK → connect WS
        print(f"🔐 [{user}] JWT OK, kết nối WS")

        # ===== 3) Kết nối WS =====
        try:
            async with websockets.connect(WS_URL, sock=sock, ssl=True, ping_interval=None) as ws:
                print(f"✅ [{user}] WS connected (conn_id={conn_id[:8]})")

                # Handshake/authorize
                try:
                    await ws.recv()  # bỏ gói chào nếu server gửi
                except Exception:
                    pass

                # gửi token (authorize)
                await ws.send(f"40/tx,{json.dumps({'token': jwt})}")

                # ngay sau khi authorize, yêu cầu server gửi thông tin user (balance, history...) để đảm bảo cập nhật balance
                try:
                    await ws.send('42/tx,["your-info"]')
                except Exception:
                    pass

                # Khi WS đã ổn định -> set 'Đang Chơi'
                await update_user_status(user, "Đang Chơi")

                # 🎁 TỰ ĐỘNG CHECK NẠP/RÚT + NHẬN QUÀ SAU KHI KẾT NỐI THÀNH CÔNG
                try:
                    import threading
                    from fetch_transactions import check_all_transactions
                    
                    def _delayed_check():
                        import time
                        time.sleep(3)
                        # Bỏ log "Auto check transactions..."
                        check_all_transactions(user)
                    
                    threading.Thread(target=_delayed_check, daemon=True).start()
                except Exception as e:
                    print(f"⚠️ [{user}] Lỗi khi schedule auto check: {e}")

                last_msg_time = time.time()
                last_ping_time = time.time()  # lưu lần cuối nhận "2"

                while True:
                    now = time.time()

                    # 🔒 Nếu bị thay thế bởi WS mới → thoát ngay
                    entry_now = active_ws.get(user)
                    if not entry_now or entry_now.get("conn_id") != conn_id:
                        print(f"🔁 [{user}] Đã có WS mới thay thế (conn_id mismatch) → thoát WS cũ.")
                        break

                    # 🔎 Nếu /api/force-check yêu cầu cập nhật balance (poke)
                    if entry_now.pop("poke_balance", None):
                        try:
                            await ws.send('42/tx,["your-info"]')
                            print(f"🔎 [{user}] Poke: yêu cầu your-info qua WS hiện tại")
                        except Exception as e:
                            print(f"⚠️ [{user}] Poke your-info lỗi: {e}")

                    # 🧭 1. Timeout toàn cục: không có bất kỳ msg nào trong 120s → reconnect
                    if now - last_msg_time > 120:
                        print(f"⏳ [{user}] Timeout 120s → reconnect")
                        break

                    # 🧭 2. Nếu 30s không nhận được ping "2" → gửi "3" để giữ kết nối
                    if now - last_ping_time > 30:
                        try:
                            await ws.send("3")
                            last_ping_time = now  # reset watchdog
                            print(f"💓 [{user}] Không thấy ping 30s → gửi pong chủ động")
                        except Exception as e:
                            print(f"⚠️ [{user}] Gửi pong lỗi: {e} → reconnect")
                            break

                    recv_task = None
                    try:
                        recv_task = asyncio.create_task(ws.recv())
                        msg = await asyncio.wait_for(recv_task, timeout=0.2)
                        last_msg_time = now

                        # 🧠 Nếu là ping từ server
                        if msg == "2":
                            await ws.send("3")
                            last_ping_time = now  # reset watchdog
                            continue

                        # 🧠 Nếu là event
                        if isinstance(msg, str) and msg.startswith("42/tx,"):
                            # xử lý event (không block): handle_event có thể là async hoặc sync
                            try:
                                # nếu handle_event là coroutine
                                maybe_coro = handle_event(user, msg)
                                if asyncio.iscoroutine(maybe_coro):
                                    # chạy không chặn vòng loop chính
                                    asyncio.create_task(maybe_coro)
                                # nếu sync thì hàm đã chạy
                            except Exception as e:
                                print(f"⚠️ [{user}] Lỗi khi gọi handle_event: {e}")

                    except asyncio.TimeoutError:
                        # Không sao, 0.2s không nhận được gì thì tiếp tục vòng lặp và đẩy queue ra WS
                        await _drain_outgoing_queue(ws, queue, user)

                    except asyncio.CancelledError:
                        # Task WS bị hủy chủ động (disconnect_user, hết tiền, thay WS mới)
                        # -> thoát êm, không in stack trace
                        raise

                    except Exception as e:
                        print(f"💥 [{user}] Lỗi WS: {repr(e)} → reconnect")
                        break

                    finally:
                        # Nếu còn recv_task đang pending -> hủy & chờ kết thúc để không rò rỉ
                        if recv_task and not recv_task.done():
                            recv_task.cancel()
                            with contextlib.suppress(Exception, asyncio.CancelledError):
                                await recv_task

        except asyncio.CancelledError:
            raise
        except (ConnectionResetError, OSError) as e:
            # Khi Ctrl+C/loop dừng, socket có thể bị reset → bỏ qua để tránh trace
            if isinstance(e, OSError) and getattr(e, "winerror", None) == 995:  # operation aborted
                return
            print(f"🔻 [{user}] WS connect reset/closed: {e}")
            return
        finally:
            with contextlib.suppress(Exception):
                sock.close()

    except asyncio.CancelledError:
        # Bị hủy từ bên ngoài -> thoát êm
        pass

    finally:
        # Chỉ dọn dẹp nếu mình vẫn là kết nối đang đăng ký
        entry = active_ws.get(user)
        if entry and entry.get("conn_id") == conn_id:
            print(f"🔻 [{user}] Đóng WS (conn_id={conn_id[:8]})")

            # 🧹 Hủy job enqueue_bets (nếu còn)
            t = entry.pop("assign_task", None)
            if t and not t.done():
                t.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await t

            # 🧹 Hủy mọi lịch hẹn call_later còn treo
            for h in entry.pop("pending_schedules", []):
                with contextlib.suppress(Exception):
                    h.cancel()

            # Xóa hàng đợi
            try:
                q = entry["queue"]
                while not q.empty():
                    q.get_nowait()
                    q.task_done()
            except Exception:
                pass

            # Gỡ khỏi active_ws
            active_ws.pop(user, None)
        else:
                # print(f"🧹 [{user}] Bỏ qua dọn dẹp (đã bị thay thế bởi WS khác).")
            pass

# ------------------- Ngắt WS cho 1 user (không pop ngay) -------------------
async def disconnect_user(user):
    entry = active_ws.get(user)
    if entry:
        print(f"🔻 Ngắt WS của {user} (cancel task, giữ entry cho task tự dọn)")
        # cancel task; handle_ws sẽ dọn dẹp entry nếu conn_id khớp
        entry_task = entry.get("task")
        if entry_task and not entry_task.done():
            entry_task.cancel()
        # không pop ở đây để tránh race condition

# 🆕 Hàm refresh accessToken
async def _refresh_access_token(username: str, proxy_str: str) -> bool:
    """
    Lấy lại accessToken từ gateway và cập nhật vào DB.
    Trả về True nếu thành công.
    """
    try:
        # Lấy password từ bảng accounts
        resp = await asyncio.to_thread(
            lambda: requests.get(f"{API_BASE}/api/accounts/{username}", timeout=5)
        )
        if resp.status_code != 200:
            print(f"⚠️ [{username}] Không lấy được account từ DB")
            return False
        
        account_data = resp.json()
        password = account_data.get("loginPass")
        if not password:
            print(f"⚠️ [{username}] Không có loginPass trong DB")
            return False
        
        # Gọi gateway để lấy accessToken mới
        print(f"🔑 [{username}] Đang lấy accessToken mới từ gateway...")
        access_token = await asyncio.to_thread(
            lambda: get_access_token(username, password, proxy_str)
        )
        
        if not access_token:
            print(f"❌ [{username}] Gateway không trả về accessToken")
            return False
        
        print(f"✅ [{username}] Lấy được accessToken mới: {access_token[:20]}...")
        
        # Cập nhật vào DB
        success = await asyncio.to_thread(
            lambda: update_access_token_to_db(username, access_token)
        )
        
        if success:
            print(f"💾 [{username}] Đã cập nhật accessToken vào DB")
            return True
        else:
            print(f"⚠️ [{username}] Không cập nhật được accessToken vào DB")
            return False
            
    except Exception as e:
        print(f"❌ [{username}] Lỗi refresh accessToken: {e}")
        return False
