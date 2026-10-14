# -*- coding: utf-8 -*-
"""
Đăng nhập XOSO66 qua Google Chrome CMS — không Playwright, không extension.

Chrome 152+ bỏ --load-extension; Chrome for Testing + profile CMS → trang trắng.
PHPSESSID là cookie HttpOnly, Chrome giữ trong RAM và chỉ ghi file Cookies khi
thoát process. Sau khi bấm Đăng nhập, đọc cookie qua cổng debug local
(Network.getCookies), không điều hướng trang.

Mở Google Chrome y hệt CMS → chờ trang/bootstrap xong → gõ user/pass bằng phím
(thật, để Vue nhận) → click «Đăng nhập» → đọc cookie → getBalance → đóng Chrome.

Dùng:
  python xoso66_chrome_ext_login.py dareyoubo
  python xoso66_chrome_manual_login.py dareyoubo
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

from xoso66_paths import apply_default_env

apply_default_env()

from xoso66_game_domain import default_base_url, resolve_base_url  # noqa: E402


def _log(msg: str) -> None:
    """Log LOGIN-UI kèm timestamp đầy đủ (đo tốc độ)."""
    now = datetime.now()
    line = f"{now.strftime('%Y-%m-%d %H:%M:%S')},{now.microsecond // 1000:03d} [LOGIN-UI] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)


def _free_tcp_port() -> int:
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _wait_cdp_port(port: int, *, timeout: float = 20.0) -> bool:
    import socket

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _cdp_page_cookies(port: int, page_url: str) -> dict[str, str]:
    """Cookie đang nằm trong Chrome (kể cả HttpOnly). Không gọi Page.navigate."""
    import json
    import urllib.request

    try:
        import websocket
    except ImportError:
        return {}
    parsed = urlparse(page_url)
    host = parsed.netloc
    origin = f"{parsed.scheme}://{host}/" if host else page_url
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as resp:
            targets = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return {}
    ws_url = ""
    fallback = ""
    for target in targets if isinstance(targets, list) else []:
        if target.get("type") != "page":
            continue
        candidate = str(target.get("webSocketDebuggerUrl") or "")
        if not candidate:
            continue
        if not fallback:
            fallback = candidate
        if host and host in str(target.get("url") or ""):
            ws_url = candidate
            break
    ws_url = ws_url or fallback
    if not ws_url:
        return {}
    ws = websocket.create_connection(ws_url, timeout=8, suppress_origin=True)
    try:
        urls = [origin]
        if page_url and page_url not in urls:
            urls.append(page_url)
        ws.send(json.dumps({
            "id": 1,
            "method": "Network.getCookies",
            "params": {"urls": urls},
        }))
        deadline = time.time() + 8
        while time.time() < deadline:
            msg = json.loads(ws.recv())
            if msg.get("id") != 1:
                continue
            out: dict[str, str] = {}
            for cookie in (msg.get("result") or {}).get("cookies") or []:
                name = str(cookie.get("name") or "").strip()
                value = str(cookie.get("value") or "").strip()
                if name and value:
                    out[name] = value
            return out
    except Exception:
        return {}
    finally:
        try:
            ws.close()
        except Exception:
            pass
    return {}


def _launch_google_chrome_cms(
    profile_dir: Path,
    proxy: str,
    url: str,
    *,
    cdp_port: int = 0,
) -> subprocess.Popen[Any]:
    """Mở đúng như CMS. Cổng debug chỉ để đọc cookie, không Playwright."""
    from xoso66_chrome_profile import launch_cms_chrome

    port = int(cdp_port or 0)
    if port > 0:
        _log(f"Google Chrome CMS + đọc cookie local :{port} (không Playwright)")
    else:
        _log("Google Chrome CMS (không CDP, không a11y-force)")
    return launch_cms_chrome(
        profile_dir, proxy, urls=[url], cdp_port=port, cdp_minimal=True
    )


def _window_pid(win: Any) -> int:
    try:
        return int(getattr(win, "ProcessId", 0) or 0)
    except Exception:
        return 0


def _pids_in_process_tree(root_pid: int) -> set[int]:
    """Mọi PID trong cây process gốc root_pid (Windows)."""
    root = int(root_pid or 0)
    if root <= 0 or sys.platform != "win32":
        return {root} if root > 0 else set()
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == -1:
            return {root}
        children: dict[int, list[int]] = {}
        try:
            pe = PROCESSENTRY32W()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if not kernel32.Process32FirstW(snap, ctypes.byref(pe)):
                return {root}
            while True:
                pid = int(pe.th32ProcessID)
                ppid = int(pe.th32ParentProcessID)
                children.setdefault(ppid, []).append(pid)
                if not kernel32.Process32NextW(snap, ctypes.byref(pe)):
                    break
        finally:
            kernel32.CloseHandle(snap)
        out: set[int] = set()
        stack = [root]
        while stack:
            cur = stack.pop()
            if cur in out:
                continue
            out.add(cur)
            stack.extend(children.get(cur, []))
        return out or {root}
    except Exception:
        return {root}


def _pids_for_chrome_profile(profile_dir: Path | None) -> set[int]:
    """chrome.exe có CommandLine chứa user-data-dir của profile CMS."""
    if profile_dir is None or sys.platform != "win32":
        return set()
    try:
        marker = str(Path(profile_dir).resolve()).lower().replace("'", "''")
    except Exception:
        return set()
    if not marker:
        return set()
    ps = (
        f"$marker = '{marker}'\n"
        "Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
        "Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains($marker) } | "
        "ForEach-Object { $_.ProcessId }\n"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=12,
        )
        out: set[int] = set()
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit():
                out.add(int(line))
        return out
    except Exception:
        return set()


def _resolve_chrome_allow_pids(
    *,
    chrome_pid: int = 0,
    profile_dir: Path | None = None,
) -> set[int]:
    """PID cửa sổ UI hợp lệ = cây process launch + mọi chrome cùng profile."""
    pids: set[int] = set()
    root = int(chrome_pid or 0)
    if root > 0:
        pids |= _pids_in_process_tree(root)
    # Mọi chrome.exe có --user-data-dir=profile (kể cả GPU/renderer sibling).
    pids |= _pids_for_chrome_profile(profile_dir)
    return pids


def _bring_window_to_front(win: Any) -> bool:
    """Đưa đúng cửa sổ Chrome CMS lên trước (tránh dính Chrome khác đang mở)."""
    ok = False
    try:
        win.SetFocus()
        ok = True
    except Exception:
        try:
            win.SetActive()
            ok = True
        except Exception:
            pass
    if sys.platform == "win32":
        try:
            import ctypes

            hwnd = int(getattr(win, "NativeWindowHandle", 0) or 0)
            if hwnd:
                user32 = ctypes.windll.user32
                # Profile CMS hay nhớ cửa sổ nhỏ. Phóng to trước khi gõ,
                # không thì ô đăng nhập bị che.
                SW_SHOWMAXIMIZED = 3
                user32.ShowWindow(hwnd, SW_SHOWMAXIMIZED)
                user32.BringWindowToTop(hwnd)
                # AttachThreadInput giúp SetForegroundWindow khi bị Windows chặn.
                fg = user32.GetForegroundWindow()
                cur_tid = user32.GetWindowThreadProcessId(fg, None)
                tgt_tid = user32.GetWindowThreadProcessId(hwnd, None)
                if cur_tid and tgt_tid and cur_tid != tgt_tid:
                    user32.AttachThreadInput(cur_tid, tgt_tid, True)
                    user32.SetForegroundWindow(hwnd)
                    user32.AttachThreadInput(cur_tid, tgt_tid, False)
                else:
                    user32.SetForegroundWindow(hwnd)
                ok = True
        except Exception:
            pass
    return ok


def _find_chrome_windows(
    auto: Any,
    *,
    include_blank: bool = False,
    allow_pids: set[int] | None = None,
) -> list[Any]:
    """Cửa sổ Google Chrome (bỏ Chrome for Testing). Chỉ lấy PID thuộc profile nếu có allow_pids."""
    out: list[Any] = []
    try:
        root = auto.GetRootControl()
    except Exception:
        return out
    for w in root.GetChildren():
        try:
            if str(getattr(w, "ClassName", "") or "") != "Chrome_WidgetWin_1":
                continue
            name = str(getattr(w, "Name", "") or "")
            if "Chrome for Testing" in name:
                continue
            if not include_blank and name.strip() in ("", "Untitled", "about:blank"):
                continue
            if include_blank and name.strip() == "":
                continue
            if allow_pids is not None:
                if not allow_pids:
                    continue
                if _window_pid(w) not in allow_pids:
                    continue
            out.append(w)
        except Exception:
            continue
    return out


def _pick_site_window(wins: list[Any]) -> Any | None:
    if not wins:
        return None
    for w in wins:
        try:
            n = str(getattr(w, "Name", "") or "")
        except Exception:
            n = ""
        if any(k in n for k in ("XOSO", "xoso", "Trang chủ", "whskxk", "whsbzk", "404", "Đăng", "Welcome")):
            return w
    return wins[0]


def _wait_any_chrome_window(auto: Any, *, timeout_sec: float = 20.0) -> Any | None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        wins = _find_chrome_windows(auto, include_blank=True)
        if wins:
            return wins[0]
        time.sleep(0.2)
    return None


def _navigate_omnibox(win: Any, auto: Any, url: str) -> bool:
    """Gõ URL vào omnibox (sau about:blank) — tránh SPA trắng khi mở URL trên cmdline."""
    try:
        win.SetFocus()
    except Exception:
        try:
            win.SetActive()
        except Exception:
            pass
    time.sleep(0.15)
    try:
        auto.SendKeys("{Ctrl}l")
        time.sleep(0.12)
        auto.SendKeys("{Ctrl}a")
        time.sleep(0.05)
        auto.SendKeys(_escape_sendkeys(str(url)), waitTime=0.008)
        time.sleep(0.08)
        auto.SendKeys("{Enter}")
        return True
    except Exception:
        return False


def _walk_controls(ctrl: Any, auto: Any, *, max_depth: int = 30):
    try:
        for c, depth in auto.WalkControl(ctrl, maxDepth=max_depth):
            yield c, depth
    except Exception:
        return


def _dismiss_restore_bubble(win: Any, auto: Any) -> None:
    try:
        win.SetFocus()
    except Exception:
        try:
            win.SetActive()
        except Exception:
            pass
    try:
        auto.SendKeys("{Esc}")
    except Exception:
        pass
    time.sleep(0.25)


def _dismiss_password_save_bubble(win: Any, auto: Any) -> bool:
    """
    Bubble «Lưu mật khẩu?» của Chrome — ưu tiên bấm Lưu / Save (giữ pass trong profile).
    Chỉ Esc khi không thấy nút Lưu.
    """
    handled = False
    try:
        win.SetFocus()
    except Exception:
        try:
            win.SetActive()
        except Exception:
            pass

    save_labels = ("Lưu", "Save", "Save password", "Lưu mật khẩu")
    for c, _d in _walk_controls(win, auto, max_depth=22):
        try:
            name = str(getattr(c, "Name", "") or "").strip()
            if name not in save_labels:
                continue
            ctype = str(getattr(c, "ControlTypeName", "") or "")
            if ctype not in (
                "ButtonControl",
                "HyperlinkControl",
                "TextControl",
                "CustomControl",
            ):
                continue
            # Tránh nhầm text tiêu đề bubble «Lưu mật khẩu?» — ưu tiên Button.
            if ctype != "ButtonControl" and name in ("Lưu mật khẩu", "Save password"):
                continue
            c.Click()
            handled = True
            time.sleep(0.2)
            break
        except Exception:
            continue

    if not handled:
        try:
            auto.SendKeys("{Esc}")
            handled = True
        except Exception:
            pass
    if handled:
        time.sleep(0.15)
    return handled


def _is_password_edit(edit: Any) -> bool:
    try:
        return bool(getattr(edit, "IsPassword", False))
    except Exception:
        return False


def _same_ui_control(a: Any, b: Any) -> bool:
    """True nếu a và b là cùng một control UIA (tránh gõ 2 lần vào một ô)."""
    if a is None or b is None:
        return False
    if a is b:
        return True
    try:
        ra, rb = getattr(a, "RuntimeId", None), getattr(b, "RuntimeId", None)
        if ra is not None and rb is not None and tuple(ra) == tuple(rb):
            return True
    except Exception:
        pass
    try:
        ba, bb = a.BoundingRectangle, b.BoundingRectangle
        if (
            ba.left == bb.left
            and ba.top == bb.top
            and ba.right == bb.right
            and ba.bottom == bb.bottom
        ):
            return True
    except Exception:
        pass
    return False


def _escape_sendkeys(text: str) -> str:
    """Escape ký tự đặc biệt của uiautomation SendKeys (+ ^ % ~ { } ( ))."""
    out: list[str] = []
    for ch in str(text):
        if ch in "+^%~{}()":
            out.append("{" + ch + "}")
        else:
            out.append(ch)
    return "".join(out)


def _type_into_edit(edit: Any, value: str, auto: Any) -> bool:
    """
    Gõ phím thật — SetValue/a11y thường không cập nhật Vue → login lỗi baseInfo / rỗng.
    """
    try:
        edit.Click()
    except Exception:
        try:
            edit.SetFocus()
        except Exception:
            return False
    time.sleep(0.05)
    try:
        auto.SendKeys("{Ctrl}a")
        time.sleep(0.03)
        auto.SendKeys("{Delete}")
        time.sleep(0.03)
        auto.SendKeys(_escape_sendkeys(str(value)), waitTime=0.005)
        time.sleep(0.05)
        return True
    except Exception:
        return False


def _click_dang_xuat(win: Any, auto: Any) -> bool:
    """Click «Đăng xuất» để mở lại form login mới."""
    for c, _depth in _walk_controls(win, auto, max_depth=35):
        try:
            name = str(getattr(c, "Name", "") or "").strip()
            if "Đăng xuất" not in name:
                continue
            ctype = str(getattr(c, "ControlTypeName", "") or "")
            if ctype in (
                "ButtonControl",
                "HyperlinkControl",
                "TextControl",
                "CustomControl",
                "GroupControl",
            ):
                c.Click()
                return True
        except Exception:
            continue
    return False


def _click_dang_nhap(win: Any, auto: Any) -> bool:
    """Click chuột vào «Đăng nhập» (không InvokePattern — giống tay hơn)."""
    candidates: list[Any] = []
    for c, _depth in _walk_controls(win, auto, max_depth=35):
        try:
            name = str(getattr(c, "Name", "") or "").strip()
            if name != "Đăng nhập":
                continue
            ctype = str(getattr(c, "ControlTypeName", "") or "")
            if ctype in (
                "ButtonControl",
                "HyperlinkControl",
                "TextControl",
                "CustomControl",
                "GroupControl",
            ):
                candidates.append(c)
        except Exception:
            continue
    for c in candidates:
        try:
            c.Click()
            return True
        except Exception:
            continue
    return False


def _collect_login_edits(win: Any, auto: Any) -> tuple[Any | None, Any | None, Any | None]:
    edits: list[Any] = []
    for c, _d in _walk_controls(win, auto, max_depth=35):
        try:
            if str(getattr(c, "ControlTypeName", "") or "") != "EditControl":
                continue
            name = str(getattr(c, "Name", "") or "").lower()
            if "address" in name or "thanh địa" in name or "omnibox" in name:
                continue
            edits.append(c)
        except Exception:
            continue

    pass_edit = None
    normals: list[Any] = []
    for e in edits:
        if _is_password_edit(e):
            pass_edit = e
        else:
            normals.append(e)
    user_edit = normals[0] if normals else None
    captcha_edit = normals[1] if len(normals) >= 2 else None
    return user_edit, pass_edit, captcha_edit


def _login_form_gone(win: Any, auto: Any) -> bool:
    """Form login biến mất (ô password + nút Đăng nhập không còn)."""
    _user, pass_edit, _cap = _collect_login_edits(win, auto)
    if pass_edit is not None:
        return False
    names = _control_names(win, auto, max_depth=25)
    if any(str(n).strip() == "Đăng nhập" for n in names):
        return False
    return True


def _looks_logged_in(win: Any, auto: Any, username: str) -> bool:
    """
    Chỉ True khi có dấu hiệu ĐÃ login.
    Trang CF / loading không có ô password → trước đây bị nhận nhầm là đã login.
    """
    if _on_cf_challenge(win, auto):
        return False
    names = _control_names(win, auto, max_depth=25)
    joined = " | ".join(names)
    if "1970" in joined:
        return False
    _user, pass_edit, _cap = _collect_login_edits(win, auto)
    if pass_edit is not None:
        return False

    has_login_btn = any(str(n).strip() == "Đăng nhập" for n in names)

    # Dấu hiệu dương: Đăng xuất (không còn form password).
    if any("Đăng xuất" in str(n) for n in names):
        return True
    # Còn nút Đăng nhập → chắc chắn chưa xong (trừ khi bubble che; xử lý bằng Esc).
    if has_login_btn:
        return False

    u = str(username or "").strip().lower()
    if u:
        for n in names:
            nn = str(n).strip()
            if nn.lower() == u:
                return True
    # Form đã biến mất + marker member (kể cả khi bubble che một phần tree).
    if any(x in joined for x in ("Kho báu", "Số dư", "Thành viên", "Nạp tiền", "Rút tiền")):
        return True
    try:
        title = str(getattr(win, "Name", "") or "")
        if u and u in title.lower():
            if any(x in joined for x in ("Kho báu", "Số dư", "Thành viên", "Đăng xuất")):
                return True
    except Exception:
        pass
    return False


def _page_ready_for_login(win: Any, auto: Any) -> bool:
    """Form login đã có + title không còn Untitled."""
    try:
        title = str(getattr(win, "Name", "") or "")
    except Exception:
        title = ""
    if not title or title in ("Untitled", "about:blank"):
        return False
    _u, pass_el, _c = _collect_login_edits(win, auto)
    return pass_el is not None


def _page_looks_blank_shell(win: Any, auto: Any) -> bool:
    """
    Shell HTML đã load (title XOSO) nhưng SPA trắng — không form, không CF, không marker.
    Trường hợp user phải F5 tay mới hiện login.
    """
    try:
        title = str(getattr(win, "Name", "") or "")
    except Exception:
        title = ""
    if not title or title in ("Untitled", "about:blank"):
        return False
    siteish = any(
        k in title
        for k in ("XOSO", "xoso", "Welcome", "whsbzk", "whskxk", "Trang chủ")
    )
    if not siteish:
        return False
    if _on_cf_challenge(win, auto):
        return False
    if _page_ready_for_login(win, auto):
        return False
    names = _control_names(win, auto, max_depth=18)
    joined = " | ".join(names)
    if "1970" in joined:
        return False
    # Có marker UI thật → không phải trắng.
    if any(n == "Đăng nhập" for n in names):
        return False
    if any(x in joined for x in ("Đăng nhập Google", "Kho báu", "Số dư", "Thành viên", "Đăng xuất")):
        return False
    return True


def _on_cf_challenge(win: Any, auto: Any) -> bool:
    names = _control_names(win, auto, max_depth=18)
    joined = " | ".join(names).lower()
    if "cloudflare" in joined or "verify you are human" in joined:
        return True
    if "xác nhận" in joined and ("chọn" in joined or "ảnh" in joined):
        return True
    try:
        title = str(getattr(win, "Name", "") or "").lower()
        if "just a moment" in title or "attention required" in title:
            return True
    except Exception:
        pass
    return False


def _control_names(win: Any, auto: Any, *, max_depth: int = 30) -> list[str]:
    names: list[str] = []
    for c, _d in _walk_controls(win, auto, max_depth=max_depth):
        try:
            n = str(getattr(c, "Name", "") or "").strip()
            if n:
                names.append(n)
        except Exception:
            continue
    return names


def _ui_is_broken_epoch(win: Any, auto: Any) -> bool:
    """Giao diện lỗi: đồng hồ 01/01/1970 = baseInfo chưa load."""
    for n in _control_names(win, auto, max_depth=22):
        if "1970" in n:
            return True
    return False


def _ui_is_healthy(win: Any, auto: Any) -> bool:
    """
    Giao diện đúng (như mở tay): có «Đăng nhập Google» / «Kho báu» / giờ thật.
    Không còn 01/01/1970.
    """
    names = _control_names(win, auto, max_depth=22)
    joined = " | ".join(names)
    if "1970" in joined:
        return False
    if any(n == "Đăng nhập Google" for n in names):
        return True
    if any("Kho báu" in n for n in names):
        return True
    for n in names:
        if "GMT" in n and "/20" in n and "1970" not in n:
            return True
    return False


def _refresh_page(win: Any, auto: Any) -> None:
    try:
        win.SetFocus()
    except Exception:
        pass
    try:
        auto.SendKeys("{F5}")
    except Exception:
        try:
            auto.SendKeys("{Ctrl}r")
        except Exception:
            pass


def _scan_post_login(win: Any, auto: Any, username: str, *, max_depth: int = 18) -> dict[str, Any]:
    """
    Một lần WalkControl sau khi bấm Đăng nhập.
    Chỉ OK khi có dấu hiệu ĐÃ login (Đăng xuất / Thành viên…) — không tin «form biến mất».
    """
    has_pass = False
    has_login_btn = False
    has_logout = False
    has_user = False
    has_member = False
    has_1970 = False
    has_cf = False
    edit_n = 0
    save_btn: Any | None = None
    u = str(username or "").strip().lower()
    save_labels = ("Lưu", "Save", "Save password")
    # Không gồm «Kho báu» — link đó có cả khi chưa đăng nhập.
    member_keys = ("Số dư", "Thành viên", "Nạp tiền", "Rút tiền")

    for c, depth in _walk_controls(win, auto, max_depth=max_depth):
        try:
            ctype = str(getattr(c, "ControlTypeName", "") or "")
            name = str(getattr(c, "Name", "") or "").strip()
            if ctype == "EditControl":
                nlow = name.lower()
                if "address" not in nlow and "thanh địa" not in nlow and "omnibox" not in nlow:
                    edit_n += 1
                    if _is_password_edit(c):
                        has_pass = True
            if name == "Đăng nhập":
                has_login_btn = True
            if "Đăng xuất" in name:
                has_logout = True
            if u and name.lower() == u:
                has_user = True
            if any(k in name for k in member_keys):
                has_member = True
            if "1970" in name:
                has_1970 = True
            nl = name.lower()
            if "cloudflare" in nl or "verify you are human" in nl:
                has_cf = True
            if (
                save_btn is None
                and name in save_labels
                and ctype in ("ButtonControl", "HyperlinkControl", "CustomControl")
            ):
                save_btn = c
        except Exception:
            continue

        if has_logout and (save_btn is not None or depth >= 10):
            break
        if has_login_btn and edit_n >= 2 and depth >= 12:
            break

    # Còn form login (nút Đăng nhập + ≥2 ô) → chưa xong (kể cả IsPassword=False).
    if has_login_btn and edit_n >= 2:
        has_pass = True

    form_gone = (not has_pass) and (not has_login_btn)
    ok = False
    if not has_cf and not has_1970 and not has_login_btn and not has_pass:
        # Ưu tiên «Đăng xuất»; username trên header khi hết form.
        if has_logout:
            ok = True
        elif has_user and edit_n == 0:
            ok = True
    return {
        "ok": ok,
        "form_gone": form_gone,
        "has_pass": has_pass,
        "has_login_btn": has_login_btn,
        "has_logout": has_logout,
        "edit_n": edit_n,
        "save_btn": save_btn,
    }


def _wait_logged_in(
    win: Any,
    auto: Any,
    username: str,
    *,
    timeout_sec: float = 8.0,
    allow_pids: set[int] | None = None,
) -> bool:
    """Chờ dấu hiệu login thật (Đăng xuất/Thành viên); bấm Lưu mật khẩu nếu có."""
    deadline = time.time() + max(1.0, float(timeout_sec))
    saved = False
    cur = win
    while time.time() < deadline:
        try:
            wins = _find_chrome_windows(auto, allow_pids=allow_pids)
            cur = _pick_site_window(wins) or cur
        except Exception:
            cur = win
        st = _scan_post_login(cur, auto, username, max_depth=18)
        btn = st.get("save_btn")
        if btn is not None and not saved:
            try:
                btn.Click()
                saved = True
            except Exception:
                try:
                    auto.SendKeys("{Esc}")
                except Exception:
                    pass
        if st.get("ok"):
            return True
        time.sleep(0.15)
    try:
        wins = _find_chrome_windows(auto, allow_pids=allow_pids)
        cur = _pick_site_window(wins) or cur
    except Exception:
        pass
    st = _scan_post_login(cur, auto, username, max_depth=20)
    if st.get("save_btn") is not None and not saved:
        try:
            st["save_btn"].Click()
        except Exception:
            pass
    return bool(st.get("ok"))


def _scan_pre_login(
    win: Any,
    auto: Any,
    username: str = "",
    *,
    max_depth: int = 20,
) -> dict[str, Any]:
    """
    Một lần WalkControl trước login.
    Chrome SPA thường KHÔNG set IsPassword trên ô mật khẩu → fallback: 2 Edit + nút Đăng nhập.
    """
    pass_el: Any | None = None
    all_edits: list[Any] = []
    has_login_btn = False
    has_google_login = False
    has_logout = False
    has_user = False
    has_member = False
    has_1970 = False
    has_cf = False
    u = str(username or "").strip().lower()
    member_keys = ("Kho báu", "Số dư", "Thành viên", "Nạp tiền", "Rút tiền")

    try:
        title = str(getattr(win, "Name", "") or "")
    except Exception:
        title = ""

    for c, depth in _walk_controls(win, auto, max_depth=max_depth):
        try:
            ctype = str(getattr(c, "ControlTypeName", "") or "")
            name = str(getattr(c, "Name", "") or "").strip()
            # Password kể cả khi không phải EditControl (Chrome đôi khi lệch type).
            try:
                if bool(getattr(c, "IsPassword", False)):
                    pass_el = c
            except Exception:
                pass
            if ctype == "EditControl":
                nlow = name.lower()
                if "address" in nlow or "thanh địa" in nlow or "omnibox" in nlow:
                    continue
                all_edits.append(c)
                if _is_password_edit(c):
                    pass_el = c
            if name == "Đăng nhập":
                has_login_btn = True
            if name == "Đăng nhập Google":
                has_google_login = True
            if "Đăng xuất" in name:
                has_logout = True
            if u and name.lower() == u:
                has_user = True
            if any(k in name for k in member_keys):
                has_member = True
            if "1970" in name:
                has_1970 = True
            nl = name.lower()
            if "cloudflare" in nl or "verify you are human" in nl:
                has_cf = True
            if "xác nhận" in nl and ("chọn" in nl or "ảnh" in nl):
                has_cf = True
        except Exception:
            continue

        if (pass_el is not None or len(all_edits) >= 2) and has_login_btn:
            break
        if has_logout and pass_el is None and depth >= 12:
            break
        if has_cf and depth >= 8:
            break

    # Fallback: 2 ô edit cạnh Đăng nhập = user + pass (IsPassword thường False trên Chrome).
    user_el: Any | None = None
    captcha_el: Any | None = None
    if pass_el is not None:
        normals = [e for e in all_edits if e is not pass_el]
        user_el = normals[0] if normals else None
        captcha_el = normals[1] if len(normals) >= 2 else None
    elif has_login_btn and len(all_edits) >= 2:
        user_el = all_edits[0]
        pass_el = all_edits[1]
        captcha_el = all_edits[2] if len(all_edits) >= 3 else None
    elif has_login_btn and has_google_login and len(all_edits) >= 2:
        user_el = all_edits[0]
        pass_el = all_edits[1]
        captcha_el = all_edits[2] if len(all_edits) >= 3 else None
    elif has_login_btn and has_google_login and len(all_edits) == 1:
        # Chỉ 1 ô — đợi thêm (tránh gán user=pass cùng control → gõ đè 2 lần).
        user_el = all_edits[0]
        pass_el = None

    has_pass = pass_el is not None
    siteish = any(
        k in title
        for k in ("XOSO", "xoso", "Welcome", "whsbzk", "whskxk", "Trang chủ", "CASINO")
    )
    title_ok = bool(title) and title not in ("Untitled", "about:blank")
    form_ready = bool(has_pass and has_login_btn and title_ok)
    # Đã thấy khu vực login (Google + Đăng nhập) dù edit khó đọc — coi gần sẵn sàng.
    if (
        not form_ready
        and has_login_btn
        and has_google_login
        and title_ok
        and len(all_edits) >= 2
    ):
        form_ready = True
        if pass_el is None:
            pass_el = all_edits[-1]
            has_pass = True
            user_el = all_edits[0]
            captcha_el = all_edits[2] if len(all_edits) >= 3 else None

    logged_in = False
    if not has_cf and not has_1970 and not has_login_btn and pass_el is None:
        if has_logout or (has_member and has_user):
            logged_in = True

    blank_shell = bool(
        siteish
        and not has_login_btn
        and not has_google_login
        and len(all_edits) == 0
        and not has_cf
        and not has_1970
    )

    return {
        "title": title[:100],
        "siteish": siteish,
        "form_ready": form_ready,
        "has_pass": has_pass,
        "has_login_btn": has_login_btn,
        "has_google_login": has_google_login,
        "edit_n": len(all_edits),
        "has_cf": has_cf,
        "has_1970": has_1970,
        "logged_in": logged_in,
        "user_el": user_el,
        "pass_el": pass_el,
        "captcha_el": captcha_el,
        "blank_shell": blank_shell,
    }


def ui_login_in_chrome(
    *,
    username: str,
    password: str,
    timeout_sec: int = 120,
    force_fill: bool = False,
    chrome_pid: int = 0,
    profile_dir: Path | None = None,
) -> dict[str, Any]:
    """Đợi form login rồi luôn gõ user/pass — không dùng mật khẩu Chrome đã lưu."""
    _ = force_fill
    try:
        import uiautomation as auto
    except ImportError:
        return {
            "ok": False,
            "error": "missing_uiautomation",
            "msg": "pip install uiautomation",
        }

    auto.SetGlobalSearchTimeout(0.25)
    deadline = time.time() + max(45, int(timeout_sec))
    last: dict[str, Any] = {"phase": "find_window"}
    clicks = 0
    filled = 0
    refresh_count = 0
    dismissed_restore = False
    typed_once = False
    cf_hint_printed = False
    blank_since = 0.0
    wait_page_since = 0.0
    last_progress = 0.0
    logout_attempted = False
    t_loop0 = time.time()
    brought_front = False
    allow_pids = _resolve_chrome_allow_pids(
        chrome_pid=chrome_pid, profile_dir=profile_dir
    )
    last_pid_refresh = 0.0
    if chrome_pid > 0 or profile_dir is not None:
        _log(
            f"Chỉ điều khiển Chrome PID tree={sorted(allow_pids)[:8]}"
            f"{'…' if len(allow_pids) > 8 else ''} (tránh nhầm cửa sổ khác)"
        )

    while time.time() < deadline:
        now = time.time()
        # Chrome spawn child muộn — refresh allow_pids mỗi ~1.2s (PowerShell tốn kém).
        if chrome_pid > 0 or profile_dir is not None:
            if (now - last_pid_refresh) >= 1.2 or not allow_pids:
                allow_pids = _resolve_chrome_allow_pids(
                    chrome_pid=chrome_pid, profile_dir=profile_dir
                )
                last_pid_refresh = now
            wins = _find_chrome_windows(
                auto, allow_pids=allow_pids if allow_pids else set()
            )
        else:
            wins = _find_chrome_windows(auto)
        win = _pick_site_window(wins)
        if win is None:
            last = {"phase": "wait_chrome", "allow_pids": len(allow_pids)}
            blank_since = 0.0
            wait_page_since = 0.0
            time.sleep(0.2)
            continue

        if not brought_front:
            title0 = ""
            try:
                title0 = str(getattr(win, "Name", "") or "")[:80]
            except Exception:
                pass
            _bring_window_to_front(win)
            brought_front = True
            _log(
                f"Đưa cửa sổ CMS lên trước pid={_window_pid(win)} title={title0!r}"
            )

        if not dismissed_restore:
            _dismiss_restore_bubble(win, auto)
            dismissed_restore = True

        # 1 scan / vòng — không gọi looks_logged_in + page_ready + blank riêng.
        st = _scan_pre_login(win, auto, username, max_depth=20)
        now = time.time()

        if st.get("logged_in") and not st.get("form_ready") and not typed_once:
            if not logout_attempted:
                _log("Chrome đã login sẵn — Đăng xuất để đăng nhập mới")
                logout_attempted = True
                if _click_dang_xuat(win, auto):
                    time.sleep(1.5)
                    continue
                _log("Không bấm được Đăng xuất — F5 lấy form login")
                _refresh_page(win, auto)
                time.sleep(2.0)
                continue
            last = {"phase": "wait_form_after_logout"}
            time.sleep(0.3)
            continue

        if st.get("has_cf"):
            if not cf_hint_printed:
                _log("Đang CF (chọn con vật nếu có) — bấm tay trong Chrome…")
                cf_hint_printed = True
            last = {"phase": "cf_challenge"}
            blank_since = 0.0
            wait_page_since = 0.0
            time.sleep(0.6)
            continue

        if not st.get("form_ready"):
            last = {"phase": "wait_page", "title": st.get("title")}
            if wait_page_since <= 0:
                wait_page_since = now
            if last_progress <= 0 or (now - last_progress) >= 2.0:
                _log(
                    f"Đợi form… {now - t_loop0:.1f}s edits={st.get('edit_n')} "
                    f"login_btn={st.get('has_login_btn')} google={st.get('has_google_login')} "
                    f"blank={st.get('blank_shell')} title={st.get('title')!r}"
                )
                last_progress = now

            # Chỉ F5 khi thật sự shell trắng — KHÔNG F5 chỉ vì siteish (title XOSO).
            need_f5 = False
            if st.get("blank_shell"):
                if blank_since <= 0:
                    blank_since = now
                if refresh_count < 4 and (now - blank_since) >= 2.0:
                    need_f5 = True
            else:
                blank_since = 0.0
                # Có title/nút nhưng chưa đọc được edit — đợi lâu hơn rồi mới F5.
                if refresh_count < 3 and (now - wait_page_since) >= 8.0:
                    need_f5 = True

            if need_f5:
                refresh_count += 1
                why = "trang trắng" if st.get("blank_shell") else "chưa đọc được ô login"
                _log(f"{why} — F5 lần {refresh_count}")
                _refresh_page(win, auto)
                blank_since = 0.0
                wait_page_since = 0.0
                last_progress = 0.0
                _log("Chờ 2.5s cho trang ổn định sau F5…")
                time.sleep(2.5)
                continue
            time.sleep(0.12)
            continue

        blank_since = 0.0
        wait_page_since = 0.0

        if st.get("has_1970"):
            last = {"phase": "wait_healthy_ui", "broken_epoch": True}
            if refresh_count < 4:
                refresh_count += 1
                _log(f"UI lỗi 01/01/1970 — F5 lần {refresh_count}")
                _refresh_page(win, auto)
                _log("Chờ 2.5s cho trang ổn định sau F5…")
                time.sleep(2.5)
            else:
                time.sleep(0.2)
            continue

        user_el = st.get("user_el")
        pass_el = st.get("pass_el")
        cap_el = st.get("captcha_el")
        if not pass_el:
            last = {"phase": "wait_form"}
            time.sleep(0.1)
            continue

        # Chỉ gõ user/pass một lần mỗi lượt (retry chỉ khi reset typed_once).
        if not typed_once:
            _log(f"Form sẵn sàng sau {now - t_loop0:.1f}s — chờ 2.5s ổn định rồi login…")
            time.sleep(2.5)
            st = _scan_pre_login(win, auto, username, max_depth=20)
            if st.get("has_1970") or st.get("has_cf") or not st.get("form_ready"):
                continue
            user_el = st.get("user_el")
            pass_el = st.get("pass_el")
            cap_el = st.get("captcha_el")
            if not pass_el:
                continue
            if _same_ui_control(user_el, pass_el):
                _log("UI chỉ thấy 1 ô edit — đợi thêm ô mật khẩu…")
                time.sleep(0.35)
                continue
            _log(f"Form sẵn sàng sau {now - t_loop0:.1f}s — gõ user/pass + Đăng nhập")
            _bring_window_to_front(win)

            ok_user = _type_into_edit(user_el, username, auto) if user_el else False
            time.sleep(0.05)
            ok_pass = _type_into_edit(pass_el, password, auto)
            if ok_user or ok_pass:
                filled += 1
                typed_once = True
            time.sleep(0.06)

        if not typed_once:
            continue

        clicked = _click_dang_nhap(win, auto)
        if not clicked:
            try:
                pass_el.Click()
                time.sleep(0.04)
                auto.SendKeys("{Enter}")
                clicked = True
            except Exception:
                clicked = False

        if clicked:
            clicks += 1
            _log(f"Đã bấm Đăng nhập (lần {clicks})")
            time.sleep(0.1)
            if _wait_logged_in(
                win, auto, username, timeout_sec=8.0, allow_pids=allow_pids
            ):
                _log("Login OK")
                return {
                    "ok": True,
                    "via": "click",
                    "clicks": clicks,
                    "filled": filled,
                    "refresh_count": refresh_count,
                }
            wins2 = _find_chrome_windows(auto, allow_pids=allow_pids)
            win2 = _pick_site_window(wins2) or win
            st2 = _scan_post_login(win2, auto, username, max_depth=18)
            if st2.get("save_btn") is not None:
                try:
                    st2["save_btn"].Click()
                except Exception:
                    pass
            if st2.get("ok"):
                _log("Login OK")
                return {
                    "ok": True,
                    "via": "click",
                    "clicks": clicks,
                    "filled": filled,
                    "refresh_count": refresh_count,
                }
            if _ui_is_broken_epoch(win2, auto):
                _log("Sau click lại 1970 — F5 rồi thử lại")
                typed_once = False
                continue
            _log("Chưa vào được — thử lại nhanh…")
            time.sleep(0.3)
            typed_once = False
        else:
            last = {"phase": "no_login_button"}

        last = {
            "phase": "retry",
            "clicks": clicks,
            "filled": filled,
            "has_captcha_field": cap_el is not None,
            "refresh_count": refresh_count,
        }
        if clicks >= 4:
            break
        time.sleep(0.2)

    return {
        "ok": False,
        "error": "ui_login_timeout",
        "last": last,
        "clicks": clicks,
        "refresh_count": refresh_count,
        "hint": (
            "UI phải giống mở tay: có form Đăng nhập / giờ thật (không 01/01/1970). "
            "Nếu CF chọn con vật — bấm tay trong Chrome."
        ),
    }


def wait_chrome_login_form_ready(
    *,
    profile_dir: Path,
    proxy: str,
    url: str,
    timeout_sec: int = 90,
) -> dict[str, Any]:
    """
    Chỉ mở Chrome + chờ form Đăng nhập sẵn sàng (không gõ, không bấm login).
    Dùng để test bước 1: trang load đúng.
    """
    try:
        import uiautomation as auto
    except ImportError:
        return {"ok": False, "error": "missing_uiautomation"}

    from xoso66_chrome_profile import mark_chrome_clean_exit, terminate_chrome

    meta: dict[str, Any] = {"url": url, "phase": "launch"}
    proc = None
    t0 = time.time()
    try:
        mark_chrome_clean_exit(profile_dir)
        _log(f"[READY-TEST] Mở Chrome URL trực tiếp: {url}")
        proc = _launch_google_chrome_cms(profile_dir, proxy, url)
        meta["pid"] = proc.pid
        auto.SetGlobalSearchTimeout(0.35)
        deadline = time.time() + max(20, int(timeout_sec))
        refresh_count = 0
        blank_since = 0.0
        wait_page_since = 0.0
        dismissed = False
        last_progress = 0.0
        brought_front = False
        allow_pids: set[int] = set()
        last_pid_refresh = 0.0

        while time.time() < deadline:
            now = time.time()
            if (now - last_pid_refresh) >= 1.2 or not allow_pids:
                allow_pids = _resolve_chrome_allow_pids(
                    chrome_pid=int(proc.pid or 0), profile_dir=profile_dir
                )
                last_pid_refresh = now
            wins = _find_chrome_windows(
                auto, allow_pids=allow_pids if allow_pids else set()
            )
            win = _pick_site_window(wins)
            if win is None:
                time.sleep(0.25)
                continue
            if not brought_front:
                _bring_window_to_front(win)
                brought_front = True
            if not dismissed:
                _dismiss_restore_bubble(win, auto)
                dismissed = True

            st = _scan_pre_login(win, auto, "", max_depth=20)
            if st.get("has_cf"):
                meta["phase"] = "cf_challenge"
                _log("[READY-TEST] Đang CF — cần bấm tay nếu có captcha")
                time.sleep(1.0)
                continue

            if st.get("form_ready") and not st.get("has_1970"):
                elapsed = time.time() - t0
                _log(f"[READY-TEST] Form login sẵn sàng sau {elapsed:.1f}s (F5={refresh_count})")
                meta.update(
                    {
                        "ok": True,
                        "elapsed_sec": round(elapsed, 2),
                        "refresh_count": refresh_count,
                        "title": st.get("title"),
                        "edit_n": st.get("edit_n"),
                    }
                )
                return meta

            now = time.time()
            if wait_page_since <= 0:
                wait_page_since = now
            if last_progress <= 0 or (now - last_progress) >= 2.0:
                _log(
                    f"[READY-TEST] Đợi form… {now - t0:.1f}s edits={st.get('edit_n')} "
                    f"login_btn={st.get('has_login_btn')} blank={st.get('blank_shell')}"
                )
                last_progress = now

            need_f5 = False
            if st.get("blank_shell"):
                if blank_since <= 0:
                    blank_since = now
                if refresh_count < 4 and (now - blank_since) >= 2.0:
                    need_f5 = True
            elif refresh_count < 3 and (now - wait_page_since) >= 8.0:
                need_f5 = True

            if need_f5:
                refresh_count += 1
                _log(f"[READY-TEST] F5 lần {refresh_count}")
                _refresh_page(win, auto)
                blank_since = 0.0
                wait_page_since = 0.0
                last_progress = 0.0
                _log("[READY-TEST] Chờ 2.5s cho trang ổn định sau F5…")
                time.sleep(2.5)
                continue
            meta["phase"] = "wait_form"
            time.sleep(0.12)

        meta["ok"] = False
        meta["error"] = "login_form_timeout"
        meta["elapsed_sec"] = round(time.time() - t0, 2)
        meta["refresh_count"] = refresh_count
        _log(f"[READY-TEST] FAIL — hết giờ ({meta['elapsed_sec']}s)")
        return meta
    finally:
        mark_chrome_clean_exit(profile_dir)
        terminate_chrome(proc)
        mark_chrome_clean_exit(profile_dir)


def login_via_chrome_ui(
    session: dict,
    *,
    profile_dir: Path,
    username: str,
    password: str,
    timeout_sec: int = 180,
    force_fill: bool = False,
) -> dict[str, Any]:
    from xoso66_chrome_profile import (
        close_chrome_gracefully,
        mark_chrome_clean_exit,
        profile_has_cf_clearance,
        profile_is_locked,
        warm_session_from_profile,
    )
    from xoso66_session import get_user_balance, persist_session, strip_identity_cookies

    proxy = str(session.get("proxy") or "").strip()
    if not proxy:
        return {"ok": False, "error": "missing_proxy"}
    if not username or not password:
        return {"ok": False, "error": "missing_credentials"}
    if profile_is_locked(profile_dir):
        return {
            "ok": False,
            "error": "profile_in_use",
            "msg": "Đóng Chrome CMS của device này rồi chạy lại.",
            "skipped": True,
        }

    base = resolve_base_url(session) or default_base_url()
    url = f"{base}/home/"
    host = urlparse(base).netloc or urlparse(default_base_url()).netloc
    meta: dict[str, Any] = {
        "method": "chrome_ui_login",
        "cdp": "cookies_only",
        "url": url,
        "force_fill": force_fill,
    }
    old_php = str((session.get("cookies") or {}).get("PHPSESSID") or "")
    meta["php_before"] = (old_php[:12] + "…") if old_php else ""
    _log(f"Mở Chrome CMS + URL trực tiếp: {url}")
    proc: subprocess.Popen[Any] | None = None
    close_chrome = True
    try:
        mark_chrome_clean_exit(profile_dir)
        had_cf = profile_has_cf_clearance(profile_dir)
        meta["had_cf_before_launch"] = had_cf

        cdp_port = _free_tcp_port()
        proc = _launch_google_chrome_cms(profile_dir, proxy, url, cdp_port=cdp_port)
        meta["pid"] = proc.pid
        meta["cdp_port"] = cdp_port
        meta["skipped_cf_wait"] = True
        meta["launch_mode"] = "direct_url"
        if not _wait_cdp_port(cdp_port, timeout=15):
            _log("Cổng đọc cookie chưa mở — sẽ chỉ đọc file Cookies")
            cdp_port = 0
        time.sleep(0.4)

        ui = ui_login_in_chrome(
            username=username,
            password=password,
            timeout_sec=max(60, int(timeout_sec) - 15),
            force_fill=force_fill,
            chrome_pid=int(proc.pid or 0),
            profile_dir=profile_dir,
        )
        meta["ui"] = ui
        if not ui.get("ok"):
            return {**meta, "ok": False, "error": ui.get("error") or "ui_login_fail", **ui}

        # Chrome còn mở: đọc PHPSESSID + cf_clearance, getBalance xong mới đóng.
        session["base_url"] = base
        close_chrome = False
        _log("Login UI OK — Chrome vẫn mở, đọc PHP/CF trong bộ nhớ rồi kiểm tra số dư…")
        last_msg = ""
        has_php = False
        has_cf = False
        for attempt in range(1, 13):
            source = "disk"
            live: dict[str, str] = {}
            if cdp_port:
                live = _cdp_page_cookies(cdp_port, url)
                if live.get("PHPSESSID"):
                    source = "memory"
            if not live.get("PHPSESSID"):
                strip_identity_cookies(session)
                warm_session_from_profile(
                    session, profile_dir, host=host, allow_identity=True
                )
                live = dict(session.get("cookies") or {})
                source = "disk"
            else:
                session["cookies"] = dict(live)
            cookies = session.get("cookies") or {}
            has_php = bool(cookies.get("PHPSESSID"))
            has_cf = bool(cookies.get("cf_clearance"))
            _log(
                f"Chrome còn mở try={attempt}/12 "
                f"PHPSESSID={has_php} cf_clearance={has_cf} via={source}"
            )
            if not (has_php and has_cf):
                last_msg = (
                    "thiếu PHPSESSID" if not has_php else "thiếu cf_clearance"
                )
                time.sleep(3.0)
                continue
            bal = get_user_balance(session, refresh=True, timeout_sec=25)
            if bal.get("ok"):
                balance = bal.get("balance")
                _log(f"Balance OK {balance} — đóng Chrome")
                try:
                    persist_session(str(session.get("id") or ""), session)
                except Exception:
                    pass
                close_chrome = True
                return {
                    **meta,
                    "ok": True,
                    "verified": True,
                    "balance": balance,
                    "ui": ui,
                    "has_php": True,
                    "has_cf": True,
                }
            raw = bal.get("raw") if isinstance(bal.get("raw"), dict) else {}
            last_msg = str(
                raw.get("msg") or bal.get("reason") or bal.get("http_status") or "session chưa hợp lệ"
            )
            _log(f"getBalance chưa OK (try {attempt}/12): {last_msg}")
            time.sleep(3.0)

        return {
            **meta,
            "ok": False,
            "verified": False,
            "chrome_left_open": True,
            "error": "getBalance_fail" if has_php and has_cf else "missing_cookie_while_open",
            "msg": last_msg or "chưa đọc được PHPSESSID và cf_clearance",
            "ui": ui,
            "has_php": has_php,
            "has_cf": has_cf,
        }
    finally:
        if close_chrome:
            mark_chrome_clean_exit(profile_dir)
            close_chrome_gracefully(proc, profile_dir, wait_unlock_sec=15)
            mark_chrome_clean_exit(profile_dir)


def login_via_chrome_extension(
    session: dict,
    *,
    profile_dir: Path,
    username: str,
    password: str,
    timeout_sec: int = 180,
) -> dict[str, Any]:
    return login_via_chrome_ui(
        session,
        profile_dir=profile_dir,
        username=username,
        password=password,
        timeout_sec=timeout_sec,
    )


def login_account_via_extension(account_id: str, *, timeout_sec: int = 180) -> dict[str, Any]:
    """Resolve CMS profile + credentials → UI login → sync cookie → Đang Chơi."""
    from xoso66_accounts_db import (
        STATUS_DANG_CHOI,
        get_account,
        set_account_status,
        update_account,
        username_for_log,
    )
    from xoso66_chrome_manual_login import sync_after_manual_login
    from xoso66_chrome_profile import mark_chrome_clean_exit, terminate_chrome_profile
    from xoso66_cms_chrome import resolve_cms_chrome_by_device
    from xoso66_session import persist_session
    from xoso66_sessions_io import load_sessions

    row = get_account(account_id) or {}
    aid = str(row.get("id") or account_id).strip()
    user = str(row.get("username") or "").strip()
    password = str(row.get("password") or "").strip()
    if not user or not password:
        sess0 = load_sessions().get(aid) or {}
        user = user or str(sess0.get("username") or sess0.get("phone") or "").strip()
        password = password or str(sess0.get("password") or "").strip()
    if not user or not password:
        return {"ok": False, "error": "missing_credentials", "account_id": aid}

    dev = str(row.get("device") or "").strip()
    if not dev:
        return {"ok": False, "error": "missing_device", "account_id": aid}

    cms = resolve_cms_chrome_by_device(dev)
    if not cms:
        return {"ok": False, "error": f"cms_not_found:{dev}", "account_id": aid}

    profile_dir = Path(str(cms.get("profile_dir") or ""))
    if not profile_dir.is_dir():
        return {"ok": False, "error": f"profile_missing:{profile_dir}", "account_id": aid}

    proxy = str(cms.get("proxy") or row.get("proxy") or "").strip()
    if not proxy:
        return {"ok": False, "error": "missing_proxy", "account_id": aid}

    update_account(aid, {"proxy": proxy})
    sess = load_sessions().get(aid) or {"id": aid}
    sess["id"] = aid
    sess["proxy"] = proxy
    sess["username"] = user
    sess["password"] = password
    persist_session(aid, sess)

    terminate_chrome_profile(profile_dir)
    time.sleep(1.2)
    mark_chrome_clean_exit(profile_dir)

    _log(f"{username_for_log(aid, row)} ({dev}) — Google Chrome + bấm Đăng nhập")

    def _once(*, force_fill: bool) -> dict[str, Any]:
        ext = login_via_chrome_ui(
            sess,
            profile_dir=profile_dir,
            username=user,
            password=password,
            timeout_sec=timeout_sec,
            force_fill=force_fill,
        )
        if ext.get("verified"):
            row2 = get_account(aid) or {}
            if str(row2.get("status") or "").strip() != STATUS_DANG_CHOI:
                set_account_status(
                    aid, STATUS_DANG_CHOI, reason="Login Chrome UI (không CDP)"
                )
            return {
                "ok": True,
                "account_id": aid,
                "balance": ext.get("balance"),
                "status": (get_account(aid) or {}).get("status"),
                "method": "chrome_ui_login",
                "ui_via": (ext.get("ui") or {}).get("via"),
            }
        if not ext.get("ok"):
            return {
                "ok": False,
                "account_id": aid,
                "error": ext.get("error") or ext.get("msg") or "login_ui_fail",
                "msg": ext.get("msg"),
                "ui_ok": bool((ext.get("ui") or {}).get("ok")) or bool(ext.get("chrome_left_open")),
                "chrome_left_open": bool(ext.get("chrome_left_open")),
                "ui_via": (ext.get("ui") or {}).get("via"),
                "ext": {k: v for k, v in ext.items() if k not in ("response",)},
            }
        time.sleep(0.8)
        sync = sync_after_manual_login(aid, profile_dir)
        if not sync.get("ok"):
            return {
                "ok": False,
                "account_id": aid,
                "error": sync.get("error") or "sync_fail",
                "msg": sync.get("msg"),
                "ui_ok": True,
                "ui_via": (ext.get("ui") or {}).get("via"),
                "sync": sync,
            }
        row2 = get_account(aid) or {}
        if str(row2.get("status") or "").strip() != STATUS_DANG_CHOI:
            set_account_status(
                aid, STATUS_DANG_CHOI, reason="Login Chrome UI (không CDP)"
            )
        return {
            "ok": True,
            "account_id": aid,
            "balance": sync.get("balance"),
            "status": (get_account(aid) or {}).get("status"),
            "method": "chrome_ui_login",
            "ui_via": (ext.get("ui") or {}).get("via"),
        }

    out = _once(force_fill=False)
    if out.get("ok"):
        return out

    # UI login OK nhưng getBalance fail → thử sync lại (không mở Chrome), rồi mới force-fill.
    msg = str(out.get("msg") or out.get("error") or "")
    if out.get("ui_ok") or "phiên" in msg.lower() or out.get("error") in (
        "sync_fail",
        "getBalance_fail",
    ):
        _log(f"Sync fail ({msg or out.get('error')}) — thử đọc cookie/getBalance lại (không mở Chrome)…")
        for i in range(1, 3):
            time.sleep(1.5)
            sync_retry = sync_after_manual_login(aid, profile_dir)
            if sync_retry.get("ok"):
                row2 = get_account(aid) or {}
                if str(row2.get("status") or "").strip() != STATUS_DANG_CHOI:
                    set_account_status(
                        aid, STATUS_DANG_CHOI, reason="Login Chrome UI (sync retry)"
                    )
                if out.get("chrome_left_open"):
                    terminate_chrome_profile(profile_dir)
                _log(f"Sync OK sau retry (lần {i}) — đóng Chrome")
                return {
                    "ok": True,
                    "account_id": aid,
                    "balance": sync_retry.get("balance"),
                    "status": (get_account(aid) or {}).get("status"),
                    "method": "chrome_ui_login",
                    "ui_via": out.get("ui_via"),
                    "sync_retry": i,
                }

        _log(
            f"Sync vẫn fail ({msg or out.get('error')}) — "
            "mở lại Chrome và BẮT BUỘC điền Đăng nhập…"
        )
        terminate_chrome_profile(profile_dir)
        time.sleep(1.0)
        mark_chrome_clean_exit(profile_dir)
        out2 = _once(force_fill=True)
        if out2.get("ok"):
            return out2
        return out2

    return out


def main() -> int:
    import argparse

    from xoso66_accounts_db import get_account, get_account_by_username, username_for_log

    ap = argparse.ArgumentParser(
        description="Auto login XOSO66 qua Google Chrome UI (không CDP)"
    )
    ap.add_argument("username", nargs="?", default="", help="username site")
    ap.add_argument("-a", "--account", default="", help="account id")
    ap.add_argument("--timeout", type=int, default=180, help="timeout giây")
    args = ap.parse_args()

    if args.account.strip():
        row = get_account(args.account.strip())
        if not row:
            print(f"Lỗi: không có account {args.account!r}", flush=True)
            return 1
    else:
        u = args.username.strip()
        if not u:
            print("Cần username hoặc -a account_id", flush=True)
            return 1
        row = get_account_by_username(u)
        if not row:
            print(f"Lỗi: không tìm thấy username {u!r}", flush=True)
            return 1

    aid = str(row["id"])
    out = login_account_via_extension(aid, timeout_sec=int(args.timeout))
    if out.get("ok"):
        print(
            f"[OK] {username_for_log(aid)} balance={out.get('balance')} "
            f"status={out.get('status')}",
            flush=True,
        )
        return 0
    print(f"[FAIL] {out.get('msg') or out.get('error') or out}", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
