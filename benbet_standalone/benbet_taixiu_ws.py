# -*- coding: utf-8 -*-
"""
WebSocket SignalR Tài Xỉu Cân Bảng (LuckyDiceHub) — đặt cược tự động.

  python benbet_taixiu_ws.py -u USER -p PASS listen
  python benbet_taixiu_ws.py -u USER -p PASS bet --side tai --amount 10000
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import threading
import time
from typing import Any, Callable

from benbet_game import (
    HUB_METHOD_BET,
    HUB_METHOD_ENTER_LOBBY,
    HUB_TAI_XIU,
    SUBDOMAIN_TAI_XIU,
    build_signalr_ws_url,
    open_tai_xiu_session,
    parse_launch_url,
    signalr_negotiate,
)
from benbet_proxy import BenbetProxy, require_socks_deps

try:
    import websocket
except ImportError:
    websocket = None  # type: ignore

HUB = HUB_TAI_XIU
DEVICE_WEB = 1
SIDE_TAI = 0
SIDE_XIU = 1
MIN_BET = 10_000  # tối thiểu server
STATE_BETTING = 0  # sessionInfo.CurrentState — được phép gửi BET
# sessionInfo.Ellapsed đếm ngược ~50→0 mỗi giây trong phase cược
BET_ELAPSED_MIN = 35  # còn ít nhất 35s mới được gửi cược
BET_ELAPSED_MAX = 45  # chờ đến khi Ellapsed <= 45 (không cược ngay lúc 50)
DEFAULT_EARLY_ELAPSED = BET_ELAPSED_MAX  # tương thích cũ


def _fix_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _parse_signalr_payload(raw: str) -> dict[str, Any] | None:
    raw = (raw or "").strip()
    if not raw or not raw.startswith("{"):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _hub_messages(obj: dict[str, Any]) -> list[dict[str, Any]]:
    msgs = obj.get("M")
    if isinstance(msgs, list):
        return [m for m in msgs if isinstance(m, dict)]
    if obj.get("M") and isinstance(obj.get("A"), list):
        return [{"M": obj.get("M"), "A": obj.get("A"), "H": obj.get("H")}]
    return []


class TaiXiuWsClient:
    """Client SignalR LuckyDiceHub — format khớp bundle Cocos."""

    def __init__(
        self,
        *,
        ws_url: str,
        access_token: str,
        session_token: str,
        username: str,
        device_type: int = DEVICE_WEB,
        quiet: bool = False,
        proxy: str | None = None,
    ) -> None:
        self.ws_url = ws_url
        self._proxy = BenbetProxy.from_string(proxy)
        self.access_token = access_token
        self.session_token = session_token
        self.username = username
        self.device_type = device_type
        self.quiet = quiet
        self._msg_id = 0
        self._ws: Any = None
        self._lock = threading.Lock()
        self.session_info: dict[str, Any] | None = None
        self.last_result: dict[str, Any] | None = None
        self.last_win: dict[str, Any] | None = None
        self.last_bet_success: dict[str, Any] | None = None
        self.balance: int | None = None
        self._handlers: dict[str, list[Callable[[dict], None]]] = {}
        self._bet_ok = threading.Event()
        self._win_ok = threading.Event()
        self._session_result_ok = threading.Event()
        self._last_result_session_id: int | None = None
        self._stop = threading.Event()
        self._bet_err: str | None = None
        self._connected = threading.Event()
        self._ws_thread: threading.Thread | None = None
        self._reconnect_backoff = 3.0

    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()
        self._connected.clear()
        self._bet_ok.set()
        self._win_ok.set()
        self._session_result_ok.set()

    def is_connected(self) -> bool:
        return self._connected.is_set() and self._ws is not None

    def _wait_connected(self, timeout: float) -> bool:
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            if self._stop.is_set():
                return False
            if self.is_connected():
                return True
            remaining = min(0.25, deadline - time.time())
            if remaining <= 0:
                break
            self._connected.wait(remaining)
        return self.is_connected()

    def refresh_ws_url(
        self,
        *,
        username: str | None = None,
        password: str | None = None,
        proxy: str | None = None,
    ) -> bool:
        """Negotiate lại ConnectionToken; nếu fail và có password → login lại."""
        import requests

        from benbet_game import (
            SUBDOMAIN_TAI_XIU,
            build_signalr_ws_url,
            open_tai_xiu_session,
            parse_launch_url,
            signalr_negotiate,
        )

        http_sess = requests.Session()
        if self._proxy:
            self._proxy.mount_session(http_sess)
        try:
            neg = signalr_negotiate(
                SUBDOMAIN_TAI_XIU,
                access_token=self.access_token,
                session_token=self.session_token,
                username=self.username,
                session=http_sess,
            )
            conn = neg.get("ConnectionToken") or neg.get("connectionToken")
            if conn:
                self.ws_url = build_signalr_ws_url(
                    connection_token=str(conn),
                    access_token=self.access_token,
                    session_token=self.session_token,
                    username=self.username,
                )
                return True
        except Exception:
            pass

        user = (username or self.username or "").strip()
        pwd = (password or "").strip()
        if not user or not pwd:
            return False
        px = proxy if proxy is not None else (self._proxy.raw if self._proxy else None)
        sess = open_tai_xiu_session(user, pwd, proxy=px)
        if not sess.get("ok"):
            return False
        params = sess.get("launch_params") or parse_launch_url(sess["launch_url"])
        self.access_token = params.get("access_token", self.access_token)
        self.session_token = params.get("session_token", self.session_token)
        self.username = params.get("username", user)
        neg = sess.get("negotiate")
        if not neg:
            try:
                neg = signalr_negotiate(
                    SUBDOMAIN_TAI_XIU,
                    access_token=self.access_token,
                    session_token=self.session_token,
                    username=self.username,
                    session=http_sess,
                )
            except Exception:
                return False
        conn = neg.get("ConnectionToken") or neg.get("connectionToken")
        if not conn:
            return False
        self.ws_url = build_signalr_ws_url(
            connection_token=str(conn),
            access_token=self.access_token,
            session_token=self.session_token,
            username=self.username,
        )
        return True

    def _close_ws_transport(self) -> None:
        ws = self._ws
        self._ws = None
        self._connected.clear()
        if ws:
            try:
                ws.close()
            except Exception:
                pass

    def _sleep(self, sec: float) -> bool:
        """Ngủ ngắn; trả False nếu đã request_stop."""
        deadline = time.time() + max(0.0, sec)
        while time.time() < deadline:
            if self._stop.is_set():
                return False
            time.sleep(min(0.2, deadline - time.time()))
        return not self._stop.is_set()

    def _wait_event(self, event: threading.Event, timeout: float) -> bool:
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            if self._stop.is_set():
                return False
            remaining = min(0.25, deadline - time.time())
            if remaining <= 0:
                break
            if event.wait(remaining):
                return True
        return False

    def on(self, event: str, fn: Callable[[dict], None]) -> None:
        self._handlers.setdefault(event.upper(), []).append(fn)

    def _emit(self, event: str, data: dict) -> None:
        for fn in self._handlers.get(event.upper(), []):
            try:
                fn(data)
            except Exception as exc:
                print(f"[handler {event}] {exc}", file=sys.stderr)

    def _next_id(self) -> int:
        with self._lock:
            i = self._msg_id
            self._msg_id += 1
            return i

    def _send_hub(self, method: str, args: list[Any]) -> None:
        if not self._ws:
            raise RuntimeError("WebSocket chua ket noi")
        body = {"M": method, "A": args, "H": HUB, "I": self._next_id()}
        text = json.dumps(body, separators=(",", ":"))
        self._ws.send(text)
        if not self.quiet:
            print(f">> {text[:500]}")

    def enter_lobby(self) -> None:
        self._send_hub(HUB_METHOD_ENTER_LOBBY, [])

    @staticmethod
    def _session_id_from(info: dict[str, Any] | None) -> int | None:
        if not info:
            return None
        sid = info.get("SessionID")
        if sid is None:
            return None
        try:
            return int(sid)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _result_session_id(result: dict[str, Any] | None) -> int | None:
        if not result:
            return None
        sid = result.get("SessionID")
        if sid is None:
            sid = result.get("sessionID")
        if sid is None:
            return None
        try:
            return int(sid)
        except (TypeError, ValueError):
            return None

    def wait_betting_phase(self, timeout: float = 55.0) -> bool:
        """Chờ phase cược (CurrentState=0) — không phân biệt đầu/cuối phiên."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._stop.is_set():
                return False
            info = self.session_info or {}
            if info.get("CurrentState") == STATE_BETTING and info.get("SessionID"):
                return True
            if not self._sleep(0.2):
                return False
        return False

    def wait_next_bet_window(
        self,
        after_session_id: int,
        *,
        min_elapsed: int = BET_ELAPSED_MIN,
        max_elapsed: int = BET_ELAPSED_MAX,
        timeout: float = 120.0,
    ) -> bool:
        """
        Chờ phiên MỚI (SessionID > after_session_id), phase cược (CurrentState=0),
        và Ellapsed trong [min_elapsed, max_elapsed] (đếm ngược, VD 45→35).
        """
        if min_elapsed > max_elapsed:
            min_elapsed, max_elapsed = max_elapsed, min_elapsed
        skip_until = int(after_session_id)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._stop.is_set():
                return False
            if not self.is_connected() and not self._wait_connected(
                min(30.0, deadline - time.time())
            ):
                return False
            info = self.session_info or {}
            sid = self._session_id_from(info)
            if sid is None or sid <= skip_until:
                if not self._sleep(0.15):
                    return False
                continue
            if info.get("CurrentState") != STATE_BETTING:
                if not self._sleep(0.15):
                    return False
                continue
            try:
                elapsed = int(info.get("Ellapsed") or 0)
            except (TypeError, ValueError):
                elapsed = 0
            if elapsed > max_elapsed:
                # Phiên mới nhưng còn sớm (46–50) — chờ Ellapsed giảm
                if not self._sleep(0.15):
                    return False
                continue
            if elapsed < min_elapsed:
                # Lỡ cửa — chờ phiên kế
                skip_until = sid
                if not self._sleep(0.15):
                    return False
                continue
            return True
        return False

    def wait_round_outcome(
        self,
        session_id: int,
        timeout: float = 75.0,
    ) -> dict[str, Any] | None:
        """
        Chờ sessionResult của phiên vừa cược (thắng/thua đều có).
        winResult (nếu thắng) có thể tới trước/sau sessionResult.
        """
        target = int(session_id)
        self._session_result_ok.clear()
        self._last_result_session_id = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._stop.is_set():
                return None
            if not self.is_connected() and not self._wait_connected(
                min(30.0, deadline - time.time())
            ):
                return None
            if self._session_result_ok.is_set():
                rid = self._last_result_session_id
                if rid == target or rid is None:
                    grace = time.time() + 2.0
                    while time.time() < grace:
                        if self._stop.is_set():
                            return None
                        if self.last_win and int(self.last_win.get("Award") or 0) > 0:
                            return {
                                "won": True,
                                "win": self.last_win,
                                "result": self.last_result,
                            }
                        if not self._sleep(0.05):
                            return None
                    return {
                        "won": False,
                        "win": None,
                        "result": self.last_result,
                    }
            cur = self._session_id_from(self.session_info)
            if cur is not None and cur > target:
                won = bool(
                    self.last_win and int(self.last_win.get("Award") or 0) > 0
                )
                return {
                    "won": won,
                    "win": self.last_win if won else None,
                    "result": self.last_result,
                }
            if not self._sleep(0.1):
                return None
        return None

    def bet(self, side: int, amount: int) -> None:
        if amount < MIN_BET:
            raise ValueError(f"amount >= {MIN_BET}")
        if side not in (SIDE_TAI, SIDE_XIU):
            raise ValueError("side: 0=Tai, 1=Xiu")
        self._bet_ok.clear()
        self._bet_err = None
        self.last_win = None
        self._win_ok.clear()
        self._send_hub(HUB_METHOD_BET, [int(amount), int(side), int(self.device_type)])

    def reset_round_events(self) -> None:
        self._bet_ok.clear()
        self._bet_err = None
        self.last_win = None
        self._win_ok.clear()

    def wait_bet_success(self, timeout: float = 12.0) -> bool:
        return self._wait_event(self._bet_ok, timeout)

    def wait_win_result(self, timeout: float = 75.0) -> dict[str, Any] | None:
        if self._wait_event(self._win_ok, timeout):
            return self.last_win
        return None

    def _on_message(self, _ws: Any, message: str | bytes) -> None:
        if isinstance(message, bytes):
            message = message.decode("utf-8", errors="replace")
        for part in message.split("\x1e"):
            part = part.strip()
            if not part:
                continue
            obj = _parse_signalr_payload(part)
            if not obj:
                continue
            if obj.get("I") is not None and not obj.get("M"):
                pong = json.dumps({"I": obj["I"]})
                self._ws.send(pong)
                continue
            for m in _hub_messages(obj):
                name = str(m.get("M") or "")
                args = m.get("A") or []
                if not self.quiet:
                    print(f"<< {name} {json.dumps(args, ensure_ascii=False)[:400]}")
                if name in ("SESSION_INFO", "sessionInfo") and args:
                    info = args[0] if isinstance(args[0], dict) else {"raw": args}
                    self.session_info = info
                    self._emit("SESSION_INFO", info)
                elif name in ("SESSION_RESULT", "sessionResult") and args:
                    self.last_result = args[0] if isinstance(args[0], dict) else {"raw": args}
                    self._last_result_session_id = self._result_session_id(self.last_result)
                    self._session_result_ok.set()
                    self._emit("SESSION_RESULT", self.last_result)
                elif name in ("BET_SUCCESS", "betSuccess"):
                    info = args[0] if args and isinstance(args[0], dict) else {}
                    self.last_bet_success = {"info": info, "A": args}
                    if len(args) > 1 and isinstance(args[1], (int, float)):
                        self.balance = int(args[1])
                    self._bet_ok.set()
                    self._emit("BET_SUCCESS", {"A": args})
                    if not self.quiet:
                        print(f"<< BET OK {json.dumps(args, ensure_ascii=False)[:300]}")
                elif name in ("WIN_RESULT", "winResult") and args:
                    win = args[0] if isinstance(args[0], dict) else {"raw": args}
                    self.last_win = win
                    if win.get("Balance") is not None:
                        self.balance = int(win["Balance"])
                    self._win_ok.set()
                    self._emit("WIN_RESULT", win)
                    if not self.quiet:
                        award = win.get("Award", 0)
                        bal = win.get("Balance", 0)
                        print(
                            f"<< THANG PHIEN Award={award} Balance={bal} "
                            f"({json.dumps(win, ensure_ascii=False)[:200]})",
                            flush=True,
                        )
                elif name in ("MESSAGE", "message") and args:
                    msg = args[0] if args else args
                    if isinstance(msg, dict):
                        txt = msg.get("Description") or msg.get("Message") or str(msg)
                    else:
                        txt = str(msg)
                    if self._bet_err is None:
                        self._bet_err = txt
                    print(f"<< MESSAGE {txt}")
                elif name == "NOTIFY_CHANGE_PHRASE" and args:
                    self._emit("NOTIFY_CHANGE_PHRASE", args[0] if args else {})

    def _on_open(self, _ws: Any) -> None:
        self._connected.set()
        if not self.quiet:
            print(f"WS connected — {HUB_METHOD_ENTER_LOBBY}", flush=True)
        self.enter_lobby()

    def _on_error(self, _ws: Any, err: Any) -> None:
        self._connected.clear()
        if not self._stop.is_set():
            print(f"WS error: {err}", file=sys.stderr, flush=True)

    def _on_close(self, _ws: Any, code: int, msg: str) -> None:
        self._connected.clear()
        if not self.quiet and not self._stop.is_set():
            print(f"WS closed: {code} {msg}", flush=True)

    def _run_forever_kwargs(self) -> dict[str, Any]:
        run_kw: dict[str, Any] = {"sslopt": {"cert_reqs": ssl.CERT_NONE}}
        if self._proxy:
            require_socks_deps(for_websocket=True)
            run_kw.update(self._proxy.websocket_run_forever_kwargs())
        return run_kw

    def _make_ws_app(self) -> Any:
        if websocket is None:
            raise RuntimeError("pip install websocket-client")
        return websocket.WebSocketApp(
            self.ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            header=[
                "Origin: https://play.3dbenbet.net",
                "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/148.0.0.0",
            ],
        )

    def _run_one_session(self) -> None:
        self._close_ws_transport()
        self._ws = self._make_ws_app()
        self._ws.run_forever(**self._run_forever_kwargs())

    def run_reconnect_loop(
        self,
        *,
        username: str | None = None,
        password: str | None = None,
        proxy: str | None = None,
    ) -> None:
        """
        Vòng lặp WS: mất kết nối → negotiate/login lại → backoff → kết nối lại.
        (Giống xoso66_minigame_ws / lc79 ws_connection.)
        """
        delay = self._reconnect_backoff
        while not self._stop.is_set():
            try:
                self._run_one_session()
            except Exception as exc:
                if not self._stop.is_set():
                    print(f"WS session lỗi: {exc}", file=sys.stderr, flush=True)
            if self._stop.is_set():
                break
            print(
                f"Mất kết nối WS — refresh token và reconnect sau {delay:.0f}s…",
                flush=True,
            )
            slept = 0.0
            while slept < delay and not self._stop.is_set():
                chunk = min(0.5, delay - slept)
                if not self._sleep(chunk):
                    break
                slept += chunk
            if self._stop.is_set():
                break
            if not self.refresh_ws_url(
                username=username, password=password, proxy=proxy
            ):
                delay = min(30.0, delay + 2.0)
                print(
                    f"Refresh WS URL thất bại — thử lại sau {delay:.0f}s",
                    flush=True,
                )
                continue
            delay = self._reconnect_backoff

    def start_background_reconnect(
        self,
        *,
        username: str | None = None,
        password: str | None = None,
        proxy: str | None = None,
    ) -> threading.Thread:
        """Chạy run_reconnect_loop trên thread daemon."""
        if self._ws_thread and self._ws_thread.is_alive():
            return self._ws_thread
        self._stop.clear()
        self._ws_thread = threading.Thread(
            target=self.run_reconnect_loop,
            kwargs={
                "username": username,
                "password": password,
                "proxy": proxy,
            },
            daemon=True,
            name="benbet-ws-reconnect",
        )
        self._ws_thread.start()
        return self._ws_thread

    def connect(self, *, block: bool = True) -> None:
        if block:
            self._run_one_session()
        else:
            t = threading.Thread(
                target=self._run_one_session,
                daemon=True,
                name="benbet-ws",
            )
            t.start()
            self._wait_connected(8.0)

    def close(self) -> None:
        self.request_stop()
        self._close_ws_transport()
        t = self._ws_thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=3.0)


def client_from_login(
    username: str,
    password: str,
    *,
    device_type: int = DEVICE_WEB,
    fresh_launch: bool = True,
    quiet: bool = False,
    proxy: str | None = None,
) -> TaiXiuWsClient:
    sess = open_tai_xiu_session(username, password, proxy=proxy)
    if not sess.get("ok"):
        raise RuntimeError(sess.get("login", {}).get("message") or "login failed")
    params = sess.get("launch_params") or parse_launch_url(sess["launch_url"])
    access = params.get("access_token", "")
    session_tok = params.get("session_token", sess.get("lt", ""))
    user = params.get("username", username)
    neg = sess.get("negotiate")
    if not neg:
        import requests

        http_sess = requests.Session()
        bp = BenbetProxy.from_string(proxy)
        if bp:
            bp.mount_session(http_sess)
        neg = signalr_negotiate(
            SUBDOMAIN_TAI_XIU,
            access_token=access,
            session_token=session_tok,
            username=user,
            session=http_sess,
        )
    conn = neg.get("ConnectionToken") or neg.get("connectionToken")
    if not conn:
        raise RuntimeError(f"negotiate khong co ConnectionToken: {neg}")
    ws_url = build_signalr_ws_url(
        connection_token=str(conn),
        access_token=access,
        session_token=session_tok,
        username=user,
    )
    return TaiXiuWsClient(
        ws_url=ws_url,
        access_token=access,
        session_token=session_tok,
        username=user,
        device_type=device_type,
        quiet=quiet,
        proxy=proxy,
    )


def side_label(side: int) -> str:
    return "Tài" if side == SIDE_TAI else "Xỉu"


def format_vnd(amount_internal: int) -> str:
    """Đơn vị nội bộ ÷ 1000 ≈ VND hiển thị trên UI."""
    return f"{amount_internal // 1000:,}".replace(",", ".")


def side_from_name(name: str) -> int:
    n = name.strip().lower()
    if n in ("tai", "t", "0"):
        return SIDE_TAI
    if n in ("xiu", "x", "1"):
        return SIDE_XIU
    raise ValueError("side: tai | xiu")


def main(argv: list[str] | None = None) -> int:
    _fix_stdout()
    ap = argparse.ArgumentParser(description="Benbet Tai Xiu WebSocket")
    ap.add_argument("-u", "--username", default="longmebaihai")
    ap.add_argument("-p", "--password", default="Valentine1")
    ap.add_argument("--device-type", type=int, default=DEVICE_WEB)
    ap.add_argument(
        "--proxy",
        default="",
        help="SOCKS5 host:port hoặc host:port:user:pass (bắt buộc cho game)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_listen = sub.add_parser("listen", help="Nghe WS (phiên / kết quả)")
    p_listen.add_argument("--sec", type=float, default=0, help="0 = chạy đến Ctrl+C")

    p_bet = sub.add_parser("bet", help="Đặt 1 lệnh rồi thoát")
    p_bet.add_argument("--side", required=True, help="tai | xiu")
    p_bet.add_argument("--amount", type=int, required=True)

    args = ap.parse_args(argv)
    proxy = (args.proxy or "").strip()
    if not proxy:
        proxy = input("SOCKS5 proxy (host:port hoặc host:port:user:pass): ").strip()
    if not proxy:
        print("Cần proxy SOCKS5 — mọi kết nối game đi qua proxy.")
        return 1
    try:
        client = client_from_login(
            args.username,
            args.password,
            device_type=args.device_type,
            proxy=proxy,
        )
    except Exception as exc:
        print(f"Loi chuan bi WS: {exc}")
        return 1

    if args.cmd == "listen":
        if args.sec and args.sec > 0:
            client.connect(block=False)
            time.sleep(args.sec)
            client.close()
        else:
            client.connect(block=True)
        return 0

    if args.cmd == "bet":
        client.connect(block=False)
        if not client.wait_betting_phase(60):
            print("Het thoi gian cho phase dat cuoc (CurrentState=0)")
            client.close()
            return 1
        info = client.session_info or {}
        print(
            f"Phien {info.get('SessionID')} Ellapsed={info.get('Ellapsed')} — gui BET...",
            flush=True,
        )
        client._bet_ok.clear()
        client._bet_err = None
        try:
            client.bet(side_from_name(args.side), args.amount)
        except Exception as exc:
            print(f"Bet loi: {exc}")
            client.close()
            return 1
        if client._bet_ok.wait(12):
            print("OK — betSuccess")
            client.close()
            return 0
        if client._bet_err:
            print(f"Server tu choi: {client._bet_err}")
        else:
            print("Khong nhan betSuccess trong 12s")
        client.close()
        return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
