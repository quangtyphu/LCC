"""
Game API Helper - Utilities chung cho các API gọi game
Chứa các hàm tái sử dụng: proxy, auth, request wrapper
"""
import sys
import io
import time
import threading

# Fix encoding cho Windows console
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from curl_cffi import requests as curl_requests
import requests as std_requests  # Fallback khi curl_cffi lỗi TLS

NODE_SERVER_URL = "http://127.0.0.1:3000"

# User đã bị đánh dấu proxy chết trong tiến trình → không lặp 5 lần retry cho mọi API.
# Lưu proxy_str lúc fail: nếu DB đổi sang proxy khác → tự mở lại và thử proxy mới.
# Half-open: sau CIRCUIT_REPROBE_SECONDS probe SOCKS5; sống lại → tự mở circuit.
# Vẫn có thể mở tay: clear_proxy_circuit / restart / HTTP 2xx / WS reconnect.
#
# value: {"proxy": str, "opened_at": float, "last_probe_at": float}
# (cũng chấp nhận str cũ = chỉ proxy key, coi opened_at = now lúc đọc lần đầu)
_PROXY_CIRCUIT_OPEN: dict[str, dict | str] = {}
_PROXY_CIRCUIT_LOCK = threading.Lock()

# Sau khi circuit mở, chờ N giây rồi probe lại (và mỗi N giây nếu vẫn chết).
CIRCUIT_REPROBE_SECONDS = 60
_PROXY_PROBE_HOST = "wtx.tele68.com"
_PROXY_PROBE_PORT = 443
_PROXY_PROBE_TIMEOUT = 5.0


def clear_proxy_circuit(username: str | None = None) -> None:
    """Xóa circuit proxy (một user hoặc toàn bộ). Gọi sau khi đã sửa proxy hoặc gỡ trạng thái Proxy Lỗi."""
    with _PROXY_CIRCUIT_LOCK:
        if username is None:
            _PROXY_CIRCUIT_OPEN.clear()
        else:
            _PROXY_CIRCUIT_OPEN.pop(username, None)


def _normalize_proxy_key(proxy_str: str | None) -> str:
    return (proxy_str or "").strip()


def _circuit_meta(entry: dict | str | None) -> dict:
    """Chuẩn hoá entry circuit (hỗ trợ format str cũ)."""
    now = time.time()
    if isinstance(entry, dict):
        return {
            "proxy": _normalize_proxy_key(entry.get("proxy")),
            "opened_at": float(entry.get("opened_at") or now),
            "last_probe_at": float(entry.get("last_probe_at") or 0),
        }
    return {
        "proxy": _normalize_proxy_key(entry if isinstance(entry, str) else ""),
        "opened_at": now,
        "last_probe_at": 0.0,
    }


def _open_proxy_circuit(username: str, proxy_str: str | None) -> None:
    with _PROXY_CIRCUIT_LOCK:
        _PROXY_CIRCUIT_OPEN[username] = {
            "proxy": _normalize_proxy_key(proxy_str),
            "opened_at": time.time(),
            "last_probe_at": 0.0,
        }


def _probe_socks5_alive(proxy_str: str, timeout: float = _PROXY_PROBE_TIMEOUT) -> bool:
    """True nếu SOCKS5 connect được tới host game (cùng cách force-check / WS)."""
    s = _normalize_proxy_key(proxy_str)
    if not s:
        return False
    try:
        import socks

        parts = s.split(":")
        if len(parts) < 4:
            return False
        host, port_s, userp = parts[0], parts[1], parts[2]
        passp = ":".join(parts[3:])
        sock = socks.socksocket()
        try:
            sock.set_proxy(socks.SOCKS5, host, int(port_s), True, userp, passp)
            sock.settimeout(timeout)
            sock.connect((_PROXY_PROBE_HOST, _PROXY_PROBE_PORT))
            return True
        finally:
            try:
                sock.close()
            except Exception:
                pass
    except Exception:
        return False


def _circuit_should_block(username: str, current_proxy: str | None) -> bool:
    """
    True = vẫn fail-fast.
    False = chưa ghim / proxy đổi / cooldown hết và probe SOCKS5 sống lại.
    """
    with _PROXY_CIRCUIT_LOCK:
        if username not in _PROXY_CIRCUIT_OPEN:
            return False
        meta = _circuit_meta(_PROXY_CIRCUIT_OPEN.get(username))
        failed_proxy = meta["proxy"]
        current = _normalize_proxy_key(current_proxy)

        if current and current != failed_proxy:
            _PROXY_CIRCUIT_OPEN.pop(username, None)
            host = current.split(":")[0] if ":" in current else current
            print(
                f"🔄 [{username}] Proxy đã đổi trên DB → mở circuit, thử proxy mới ({host})",
                flush=True,
            )
            return False

        now = time.time()
        anchor = meta["last_probe_at"] or meta["opened_at"]
        wait_left = CIRCUIT_REPROBE_SECONDS - (now - anchor)
        if wait_left > 0:
            return True

        # Half-open: probe SOCKS5 (giữ lock ngắn — probe có thể tới ~5s; nhả lock khi probe)
        probe_proxy = current or failed_proxy
        meta["last_probe_at"] = now
        _PROXY_CIRCUIT_OPEN[username] = meta

    if not probe_proxy:
        return True

    alive = _probe_socks5_alive(probe_proxy)
    if alive:
        with _PROXY_CIRCUIT_LOCK:
            # Chỉ mở nếu vẫn cùng proxy (tránh race với đổi proxy / mở lại circuit)
            cur = _PROXY_CIRCUIT_OPEN.get(username)
            if cur is not None and _circuit_meta(cur)["proxy"] == _normalize_proxy_key(probe_proxy):
                _PROXY_CIRCUIT_OPEN.pop(username, None)
        host = probe_proxy.split(":")[0] if ":" in probe_proxy else probe_proxy
        print(
            f"🔄 [{username}] Proxy sống lại (probe {_PROXY_PROBE_HOST}) → mở circuit ({host})",
            flush=True,
        )
        # Proxy Lỗi → Đang Chơi để watcher WS nhận lại (chỉ Đang Chơi mới vào active list)
        try:
            info = curl_requests.get(f"{NODE_SERVER_URL}/api/users/{username}", timeout=5)
            if info.status_code == 200:
                st = ((info.json() or {}).get("status") or "").strip()
                if st == "Proxy Lỗi":
                    curl_requests.put(
                        f"{NODE_SERVER_URL}/api/users/{username}",
                        json={"status": "Đang Chơi"},
                        timeout=5,
                    )
                    print(f"✅ [{username}] Proxy sống → status Proxy Lỗi → Đang Chơi", flush=True)
        except Exception as e:
            print(f"⚠️ [{username}] Không cập nhật status sau probe sống: {e}", flush=True)
        return False

    print(
        f"⛔ [{username}] Circuit vẫn mở — proxy probe fail, chờ {CIRCUIT_REPROBE_SECONDS}s rồi thử lại",
        flush=True,
    )
    return True


# Import jwt_manager để refresh token
try:
    from jwt_manager import refresh_jwt_and_token
except ImportError:
    def refresh_jwt_and_token(username: str) -> bool:
        print(f"WARNING: Không tìm thấy jwt_manager.py", flush=True)
        return False


def _credential_usable(val) -> bool:
    """None, rỗng, hoặc chuỗi 'null'/'none' (từ DB) → không dùng được, cần refresh."""
    if val is None:
        return False
    if isinstance(val, str):
        s = val.strip()
        return bool(s) and s.lower() not in ("null", "none")
    return True


def _normalize_secret(val):
    if val is None:
        return None
    if isinstance(val, str):
        s = val.strip()
        if not s or s.lower() in ("null", "none"):
            return None
        return s
    return str(val) if val else None


def build_proxies(proxy_str: str) -> dict | None:
    """
    Parse proxy string thành dict cho requests.
    
    Args:
        proxy_str: Format "host:port:user:pass"
    
    Returns:
        {"http": "socks5h://...", "https": "socks5h://..."}
    """
    if not proxy_str:
        return None
    try:
        host, port, userp, passp = proxy_str.split(":")
        proxy_auth = f"{userp}:{passp}@{host}:{port}"
        proxy_url = f"socks5h://{proxy_auth}"
        return {"http": proxy_url, "https": proxy_url}
    except Exception:
        return None


def get_user_auth(username: str) -> tuple | None:
    """
    Lấy thông tin auth từ DB local.
    
    Args:
        username: Username trong DB
    
    Returns:
        (proxy_str, jwt, access_token, nickname) hoặc None nếu lỗi
    """
    try:
        resp = curl_requests.get(f"{NODE_SERVER_URL}/api/users/{username}", timeout=5)
        if resp.status_code != 200:
            return None
        
        user = resp.json()
        # Không chặn theo status "Proxy Lỗi": sau khi đổi proxy trên DB, WS vẫn vào được
        # nhưng HTTP API cần đọc proxy mới; chặn ở đây sẽ luôn trả no_auth / proxy_exhausted.

        proxy_str = user.get("proxy")
        if not proxy_str:
            return None

        jwt = _normalize_secret(user.get("jwt"))
        access_token = _normalize_secret(user.get("accessToken"))
        nickname = user.get("nickname", "")

        return (proxy_str, jwt, access_token, nickname)
    
    except Exception:
        return None


def build_common_headers(jwt: str, user_agent: str = None) -> dict:
    """
    Tạo headers chuẩn cho API game.
    
    Args:
        jwt: JWT token
        user_agent: Custom User-Agent (optional)
    
    Returns:
        dict headers
    """
    if not user_agent:
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
    
    return {
        "accept": "*/*",
        "accept-language": "vi-VN,vi;q=0.9",
        "authorization": f"Bearer {jwt}",
        "content-type": "application/json",
        "origin": "https://play.lc79.bet",
        "referer": "https://play.lc79.bet/",
        "user-agent": user_agent,
        "sec-ch-ua": '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-mobile": "?0",
    }


def build_common_params(access_token: str) -> dict:
    """
    Tạo query params chuẩn cho API game.
    
    Args:
        access_token: Access token
    
    Returns:
        dict params
    """
    return {
        "cp": "R",
        "cl": "R",
        "pf": "web",
        "at": access_token
    }


def game_request_with_retry_ex(
    username: str,
    method: str,
    url: str,
    params: dict = None,
    extra_headers: dict = None,
    json_data: dict = None,
    timeout: int = 20,
) -> tuple[curl_requests.Response | None, str | None]:
    """
    Giống game_request_with_retry nhưng trả thêm failure_tag để caller dừng sớm (vd. proxy_exhausted).

    failure_tag: None (ok), "proxy_exhausted", "no_auth", "auth", "error"
    """
    # 1. Lấy auth info trước — để phát hiện proxy đã đổi trên DB và tự mở circuit
    auth = get_user_auth(username)
    if not auth:
        if username in _PROXY_CIRCUIT_OPEN:
            return None, "proxy_exhausted"
        print(f"❌ [{username}] Không lấy được auth info", flush=True)
        return None, "no_auth"

    proxy_str, jwt, access_token, _ = auth

    if _circuit_should_block(username, proxy_str):
        return None, "proxy_exhausted"

    if not _credential_usable(jwt) or not _credential_usable(access_token):
        if refresh_jwt_and_token(username):
            auth2 = get_user_auth(username)
            if auth2:
                proxy_str, jwt, access_token, _ = auth2
                if _circuit_should_block(username, proxy_str):
                    return None, "proxy_exhausted"
        if not _credential_usable(jwt) or not _credential_usable(access_token):
            print(
                f"❌ [{username}] Không lấy được JWT/accessToken sau khi thử đăng nhập",
                flush=True,
            )
            return None, "no_auth"

    # 2. Setup proxy
    proxies = build_proxies(proxy_str)
    if not proxies:
        print(f"❌ [{username}] Proxy không hợp lệ", flush=True)
        return None, "error"

    # 3. Build headers & params
    headers = build_common_headers(jwt)
    if extra_headers:
        headers.update(extra_headers)
    common_params = build_common_params(access_token)

    # Merge params
    if params:
        common_params.update(params)

    def _do_request(use_fallback: bool = False, extra_timeout: int = 0):
        """use_fallback=True: dùng std_requests khi curl_cffi lỗi. extra_timeout: cộng thêm khi fallback do timeout."""
        req = std_requests if use_fallback else curl_requests
        m = method.upper()
        t = timeout + extra_timeout if use_fallback and extra_timeout else timeout
        kwargs = dict(params=common_params, headers=headers, proxies=proxies, timeout=t)
        if json_data:
            kwargs["json"] = json_data
        if not use_fallback:
            kwargs["impersonate"] = "chrome120"
        if m == "GET":
            return req.get(url, **kwargs)
        if m == "POST":
            return req.post(url, **kwargs)
        if m == "PUT":
            return req.put(url, **kwargs)
        print(f"❌ Method không hợp lệ: {method}", flush=True)
        return None

    resp = None
    proxy_exhausted = False
    for attempt in range(1, 6):
        try:
            resp = _do_request(use_fallback=False)
            break
        except Exception as e:
            msg = str(e).lower()
            # TLS (35), timeout (28), connection closed (56), HTTP2 framing (16) → thử fallback requests
            need_fallback = (
                "curl: (35)" in msg or "boringssl" in msg or "invalid library" in msg or "ssl_error_syscall" in msg
                or "curl: (28)" in msg  # timeout
                or "curl: (56)" in msg  # connection closed
                or "curl: (16)" in msg  # HTTP2 framing layer
            )
            if need_fallback:
                try:
                    print(f"⚠️ [{username}] curl_cffi lỗi → thử requests chuẩn...", flush=True)
                    extra = 15 if "curl: (28)" in msg else 0  # timeout → thêm 15s cho fallback
                    resp = _do_request(use_fallback=True, extra_timeout=extra)
                    break
                except Exception as e2:
                    print(f"❌ [{username}] Fallback requests cũng lỗi: {e2}", flush=True)
                    return None, "error"
            proxy_closed = "connection to proxy closed" in msg or "curl: (97" in msg
            if proxy_closed:
                print(f"❌ [{username}] Lỗi proxy (attempt {attempt}/5): {e}", flush=True)
                if attempt == 5:
                    proxy_exhausted = True
                    _open_proxy_circuit(username, proxy_str)
                    try:
                        curl_requests.put(f"{NODE_SERVER_URL}/api/users/{username}", json={"status": "Proxy Lỗi"}, timeout=5)
                    except Exception:
                        pass
                    print(f"⚠️ [{username}] Proxy Lỗi sau 5 lần thử", flush=True)
                time.sleep(1)
                continue
            print(f"❌ [{username}] Lỗi request: {e}", flush=True)
            return None, "error"

    if resp is None:
        return None, "proxy_exhausted" if proxy_exhausted else "error"

    # 5. Auto-retry nếu 401/403
    if resp.status_code in (401, 403):

        if refresh_jwt_and_token(username):
            # Lấy lại token mới
            auth2 = get_user_auth(username)
            if auth2:
                proxy2, jwt2, access_token2, _ = auth2
                if _circuit_should_block(username, proxy2):
                    return None, "proxy_exhausted"
                # Proxy có thể đã đổi khi refresh — dùng proxy mới nếu khác
                if _normalize_proxy_key(proxy2) != _normalize_proxy_key(proxy_str):
                    proxy_str = proxy2
                    proxies = build_proxies(proxy_str)
                    if not proxies:
                        print(f"❌ [{username}] Proxy không hợp lệ sau refresh", flush=True)
                        return None, "error"
                headers["authorization"] = f"Bearer {jwt2}"
                common_params["at"] = access_token2

                # Retry với token mới
                try:
                    resp = _do_request(use_fallback=False)
                except Exception as e:
                    err_lower = str(e).lower()
                    need_fb = "curl: (35)" in err_lower or "boringssl" in err_lower or "invalid library" in err_lower or "curl: (28)" in err_lower or "curl: (56)" in err_lower or "curl: (16)" in err_lower
                    if need_fb:
                        try:
                            ex = 15 if "curl: (28)" in err_lower else 0
                            resp = _do_request(use_fallback=True, extra_timeout=ex)
                        except Exception:
                            resp = None
                    else:
                        resp = None
                    if resp is None:
                        print(f"❌ [{username}] Lỗi retry: {e}", flush=True)
                        return None, "error"
        else:
            print(f"❌ [{username}] Không refresh được token", flush=True)
            return None, "auth"

    if resp is not None and 200 <= resp.status_code < 300:
        clear_proxy_circuit(username)

    return resp, None


def game_request_with_retry(
    username: str,
    method: str,
    url: str,
    params: dict = None,
    extra_headers: dict = None,
    json_data: dict = None,
    timeout: int = 20
) -> curl_requests.Response | None:
    """
    Gọi API game với auto-retry khi token hết hạn (401/403).

    Args:
        username: Username để lấy auth
        method: "GET", "POST", hoặc "PUT"
        url: URL đầy đủ của API
        params: Query params (sẽ merge với common params)
        json_data: Body JSON (cho POST/PUT)
        timeout: Timeout seconds

    Returns:
        Response object hoặc None nếu lỗi
    """
    resp, _ = game_request_with_retry_ex(
        username,
        method,
        url,
        params=params,
        extra_headers=extra_headers,
        json_data=json_data,
        timeout=timeout,
    )
    return resp


def update_user_balance(username: str, new_balance: float) -> bool:
    """
    Cập nhật balance vào DB local.
    
    Args:
        username: Username
        new_balance: Balance mới
    
    Returns:
        True nếu thành công
    """
    try:
        resp = curl_requests.put(
            f"{NODE_SERVER_URL}/api/users/{username}",
            json={"balance": new_balance},
            timeout=5
        )
        return resp.status_code == 200
    except Exception as e:
        print(f"⚠️ [{username}] Lỗi cập nhật balance: {e}", flush=True)
        return False
