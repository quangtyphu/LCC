# ws_connection.py  (CẬP NHẬT)
import os
import sys
os.environ['PYTHONUNBUFFERED'] = '1'
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

import asyncio
import json
import time
import socks
import websockets
import requests
import contextlib

from constants import WS_URL, active_ws
from jwt_manager import refresh_jwt
from ws_events import handle_event  # import xử lý event
from game_login import get_access_token, update_access_token_to_db
from ws_cleanup import close_ws_socks_clean
from socks_ws_gate import socks_ws_slot

API_BASE = "http://127.0.0.1:3000"  # đổi thành URL server.js của bạn


# Dùng selector loop trên Windows để hỗ trợ socks.socksocket
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


# ------------------- HỖ TRỢ: chạy requests blocking trong thread để không block event loop ----------
async def _requests_put(path, json_data, timeout=5):
    return await asyncio.to_thread(lambda: requests.put(f"{API_BASE}{path}", json=json_data, timeout=timeout))


async def _fetch_user_proxy_from_db(username: str) -> str | None:
    """Lấy chuỗi proxy mới nhất từ Node API (dùng trước mỗi lần thử WS)."""

    def _get():
        try:
            r = requests.get(f"{API_BASE}/api/users/{username}", timeout=5)
            if r.status_code != 200:
                return None
            p = r.json().get("proxy")
            if p is None or not str(p).strip():
                return None
            return str(p).strip()
        except Exception:
            return None

    return await asyncio.to_thread(_get)


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

    # Cờ theo dõi để phân biệt đóng chủ động vs rớt kết nối
    connected_ws = False
    intentional_close = False
    should_fast_reconnect = False
    exit_reason = None
    connected_at = None

    try:
        jwt = acc.get("jwt")

        # ===== 1) Proxy check trước với retry backoff — mỗi attempt đọc proxy mới từ DB =====
        backoffs = [0, 15, 30, 60, 120]  # nhanh hơn, vẫn 5 lần
        proxy_ok = False
        proxy_str = None
        host = port = puser = ppass = None
        for attempt, delay in enumerate(backoffs, start=1):
            if delay:
                await asyncio.sleep(delay)
            proxy_str = await _fetch_user_proxy_from_db(user)
            if not proxy_str:
                print(f"⚠️ [{user}] Không lấy được proxy từ DB (attempt {attempt})")
                continue
            acc["proxy"] = proxy_str
            try:
                host, port, puser, ppass = proxy_str.split(":")
                port = int(port)
            except Exception:
                print(f"🔐 [{user}] Proxy format lỗi (attempt {attempt})")
                continue
            test_sock = socks.socksocket()
            test_sock.set_proxy(socks.SOCKS5, host, port, True, puser, ppass)
            test_sock.setblocking(True)
            try:
                test_sock.connect(("wtx.tele68.com", 443))
                proxy_ok = True
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
        try:
            sock.connect(("wtx.tele68.com", 443))
        except Exception:
            print(f"🔐 [{user}] Đã Kết Nối Proxy ( Proxy Lỗi )")
            await update_user_status(user, "Proxy Lỗi")
            await close_ws_socks_clean(None, sock)
            return

        # ===== 2) JWT — bỏ test_token (mở WS 2 lần/user gây 10038); authorize trên WS chính =====
        jwt = acc.get("jwt")
        if not jwt:
            try:
                new_jwt = await asyncio.to_thread(lambda: refresh_jwt(user))
                if new_jwt:
                    jwt = new_jwt
                    acc["jwt"] = jwt
                    await _requests_put(f"/api/users/{user}", {"jwt": jwt}, timeout=5)
                else:
                    print(f"❌ [{user}] Không có JWT")
                    await update_user_status(user, "Token Lỗi")
                    await close_ws_socks_clean(None, sock)
                    return
            except Exception as e:
                print(f"❌ [{user}] Lỗi refresh JWT: {e}")
                await update_user_status(user, "Token Lỗi")
                await close_ws_socks_clean(None, sock)
                return

        print(f"🔐 [{user}] JWT OK, kết nối WS")

        # HTTP API (lịch sử / mission / VIP) dùng game_api_helper: mở circuit + full_check sau khi proxy+JWT đã OK
        try:
            from game_api_helper import clear_proxy_circuit
            clear_proxy_circuit(user)
        except Exception as e:
            print(f"⚠️ [{user}] clear_proxy_circuit: {e}", flush=True)
        try:
            from user_full_check_service import user_full_check_logic
            import threading

            def _run_full_check():
                try:
                    user_full_check_logic(user)
                except Exception as e:
                    print(f"⚠️ [{user}] Lỗi khi chạy user_full_check_logic: {e}")

            threading.Thread(target=_run_full_check, daemon=True).start()
        except Exception as e:
            print(f"⚠️ [{user}] Lỗi import hoặc chạy user_full_check_logic: {e}")

        # ===== 3) Kết nối WS =====
        ws = None
        try:
            async with socks_ws_slot():
                ws = await websockets.connect(WS_URL, sock=sock, ssl=True, ping_interval=None)
            connected_ws = True
            # Handshake/authorize
            try:
                await ws.recv()  # bỏ gói chào nếu server gửi
            except Exception:
                pass

            # gửi token (authorize)
            await ws.send(f"40/tx,{json.dumps({'token': jwt})}")

            connected_at = time.time()
            entry["ws_connected_at"] = connected_at
            entry["last_msg_at"] = connected_at
            try:
                from ws_manager import notify_ws_connected
                notify_ws_connected(user)
            except Exception:
                pass

            last_msg_time = connected_at
            last_ping_time = connected_at

            # Reader riêng: KHÔNG hủy ws.recv() mỗi 0.2s (Windows selector + SOCKS → WinError 10038).
            inbound: asyncio.Queue = asyncio.Queue(maxsize=256)

            async def _ws_reader() -> None:
                try:
                    while True:
                        msg = await ws.recv()
                        await inbound.put(msg)
                except Exception:
                    pass
                finally:
                    with contextlib.suppress(Exception):
                        await inbound.put(None)

            reader_task = asyncio.create_task(_ws_reader())
            try:
                while True:
                    now = time.time()

                    entry_now = active_ws.get(user)
                    if not entry_now or entry_now.get("conn_id") != conn_id:
                        intentional_close = True
                        exit_reason = "replaced"
                        break

                    if entry_now.pop("poke_balance", None):
                        try:
                            await ws.send('42/tx,["your-info"]')
                            print(f"🔎 [{user}] Poke: yêu cầu your-info qua WS hiện tại")
                        except Exception as e:
                            print(f"⚠️ [{user}] Poke your-info lỗi: {e}")

                    if now - last_msg_time > 120:
                        print(f"⏳ [{user}] Timeout 120s → reconnect")
                        exit_reason = "timeout"
                        break

                    if now - last_ping_time > 30:
                        try:
                            await ws.send("3")
                            last_ping_time = now
                        except Exception as e:
                            print(f"⚠️ [{user}] Gửi pong lỗi: {e} → reconnect")
                            exit_reason = "send_error"
                            break

                    try:
                        msg = await asyncio.wait_for(inbound.get(), timeout=0.2)
                    except asyncio.TimeoutError:
                        await _drain_outgoing_queue(ws, queue, user)
                        continue

                    if msg is None:
                        exit_reason = "recv_error"
                        break

                    last_msg_time = now
                    entry_now["last_msg_at"] = now

                    if msg == "2":
                        await ws.send("3")
                        last_ping_time = now
                        continue

                    if isinstance(msg, str) and msg.startswith("42/tx,"):
                        try:
                            maybe_coro = handle_event(user, msg)
                            if asyncio.iscoroutine(maybe_coro):
                                asyncio.create_task(maybe_coro)
                        except Exception as e:
                            print(f"⚠️ [{user}] Lỗi khi gọi handle_event: {e}")
                            import traceback
                            traceback.print_exc()
            except asyncio.CancelledError:
                raise
            finally:
                reader_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reader_task

        except asyncio.CancelledError:
            intentional_close = True
            raise
        except (ConnectionResetError, OSError) as e:
            winerr = getattr(e, "winerror", None)
            if winerr in (995, 10038):
                return
            exit_reason = "connection_error"
        finally:
            if connected_ws and not intentional_close and exit_reason:
                should_fast_reconnect = True
            await close_ws_socks_clean(ws, sock, "tx", sock_handoff=(ws is not None))
            ws = None
            sock = None

    except asyncio.CancelledError:
        # Bị hủy từ bên ngoài -> thoát êm
        pass

    finally:
        # Chỉ dọn dẹp nếu mình vẫn là kết nối đang đăng ký
        entry = active_ws.get(user)
        if entry and entry.get("conn_id") == conn_id:
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

        should_reconnect = locals().get("should_fast_reconnect", False)
        if should_reconnect and user not in active_ws:
            try:
                from ws_manager import schedule_reconnect_after_drop
                schedule_reconnect_after_drop(
                    user,
                    acc,
                    exit_reason or "unknown",
                    connected_at=connected_at,
                )
            except Exception as e:
                print(f"❌ [{user}] Lỗi schedule reconnect: {e}", flush=True)
        elif intentional_close:
            try:
                from ws_manager import cancel_pending_reconnect
                cancel_pending_reconnect(user)
            except Exception:
                pass

# ------------------- Ngắt WS cho 1 user (không pop ngay) -------------------
async def disconnect_user(user):
    try:
        from ws_manager import cancel_pending_reconnect
        cancel_pending_reconnect(user)
    except Exception:
        pass
    entry = active_ws.get(user)
    if entry:
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
