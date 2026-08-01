# -*- coding: utf-8 -*-
"""
Chrome profile CMS — mở giống CMS (KHÔNG CDP), đọc cookie, tránh CF /__verify/check.

Vì sao Playwright/CDP bị CF chọn con vật:
  - CMS mở chrome.exe với --user-data-dir + --proxy-server, KHÔNG --remote-debugging-port
  - Script cũ bật --remote-debugging-port + connect_over_cdp + page.goto() → CF đánh bot
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from xoso66_session import BASE_URL

_SITE_HOST = urlparse(BASE_URL).netloc


def _cookie_host_likes(host: str = _SITE_HOST) -> tuple[str, ...]:
    """Pattern LIKE cho cookie host (subdomain + domain gốc, vd. .whskxk1.com)."""
    h = str(host or _SITE_HOST).strip().lower()
    likes = [f"%{h}%", f"%.{h}%"]
    parts = h.split(".")
    if len(parts) > 2:
        likes.append(f"%.{'.'.join(parts[1:])}%")
    if len(parts) >= 2:
        likes.append(f"%.{'.'.join(parts[-2:])}%")
    return tuple(dict.fromkeys(likes))


def _browser_isolate_dir() -> Path:
    root = Path(__file__).resolve().parent.parent / "browser_isolate"
    if not root.is_dir():
        raise RuntimeError("Thiếu browser_isolate")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root


def _cookies_db_path(profile_dir: Path) -> Path | None:
    for rel in ("Default/Network/Cookies", "Default/Cookies"):
        p = profile_dir / rel
        if p.is_file():
            return p
    return None


def _chrome_aes_key(profile_dir: Path) -> bytes | None:
    """Khóa giải cookie Chrome (Windows DPAPI + Local State)."""
    ls = profile_dir / "Local State"
    if not ls.is_file():
        return None
    try:
        data = json.loads(ls.read_text(encoding="utf-8"))
        enc_key = data.get("os_crypt", {}).get("encrypted_key")
        if not enc_key:
            return None
        import base64

        raw = base64.b64decode(enc_key)
        if raw[:5] != b"DPAPI":
            return None
        if sys.platform != "win32":
            return None
        import ctypes
        import ctypes.wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        blob_in = DATA_BLOB(len(raw[5:]), ctypes.cast(ctypes.create_string_buffer(raw[5:], len(raw[5:])), ctypes.POINTER(ctypes.c_char)))
        blob_out = DATA_BLOB()
        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        ):
            return None
        key = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return key
    except Exception:
        return None


def _decrypt_cookie_value(encrypted: bytes, key: bytes | None) -> str:
    if not encrypted:
        return ""
    try:
        if encrypted[:3] == b"v10" and key:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            nonce = encrypted[3:15]
            data = encrypted[15:]
            plain = AESGCM(key).decrypt(nonce, data, None)
            # Chrome 127+ thêm 32 byte hash đầu plaintext.
            if len(plain) > 32:
                plain = plain[32:]
            return plain.decode("utf-8", errors="replace")
        if sys.platform == "win32":
            import ctypes
            import ctypes.wintypes

            class DATA_BLOB(ctypes.Structure):
                _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

            blob_in = DATA_BLOB(len(encrypted), ctypes.cast(ctypes.create_string_buffer(encrypted, len(encrypted)), ctypes.POINTER(ctypes.c_char)))
            blob_out = DATA_BLOB()
            if ctypes.windll.crypt32.CryptUnprotectData(
                ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
            ):
                out = ctypes.string_at(blob_out.pbData, blob_out.cbData)
                ctypes.windll.kernel32.LocalFree(blob_out.pbData)
                return out.decode("utf-8", errors="replace")
    except Exception:
        pass
    return ""


def _copy_file_shared_win(src: Path, dst: Path) -> bool:
    """Copy file khi Chrome đang giữ lock (Windows FILE_SHARE_READ|WRITE|DELETE)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        expected = 0
        try:
            expected = int(src.stat().st_size)
        except OSError:
            pass

        kernel32 = ctypes.windll.kernel32
        CreateFileW = kernel32.CreateFileW
        CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        CreateFileW.restype = wintypes.HANDLE
        GENERIC_READ = 0x80000000
        OPEN_EXISTING = 3
        FILE_ATTRIBUTE_NORMAL = 0x80
        FILE_SHARE_ALL = 0x07
        INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

        handle = CreateFileW(
            str(src),
            GENERIC_READ,
            FILE_SHARE_ALL,
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if handle == INVALID_HANDLE_VALUE:
            return False
        try:
            size = ctypes.c_int64()
            file_size = 0
            if kernel32.GetFileSizeEx(handle, ctypes.byref(size)) and size.value > 0:
                file_size = int(size.value)
            elif expected > 0:
                file_size = expected
            else:
                return False
            buf = (ctypes.c_char * file_size)()
            nread = wintypes.DWORD()
            if not kernel32.ReadFile(handle, buf, file_size, ctypes.byref(nread), None):
                return False
            if nread.value <= 0:
                return False
            dst.write_bytes(bytes(buf)[: int(nread.value)])
            return True
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def _copy_cookies_db_for_read(db: Path) -> Path | None:
    """Chrome giữ lock file Cookies — copy sang temp (kèm -wal/-shm) để đọc khi Chrome đang mở."""
    import shutil
    import tempfile

    fd, tmp = tempfile.mkstemp(suffix=".cookies")
    os.close(fd)
    tmp_path = Path(tmp)
    copied_main = False

    for suffix in ("", "-wal", "-shm", "-journal"):
        src = Path(str(db) + suffix) if suffix else db
        if not src.is_file():
            continue
        dst = Path(str(tmp_path) + suffix) if suffix else tmp_path
        ok = False
        if sys.platform == "win32":
            ok = _copy_file_shared_win(src, dst)
        if not ok:
            try:
                shutil.copy2(src, dst)
                ok = dst.is_file() and dst.stat().st_size > 0
            except Exception:
                ok = False
        if suffix == "" and ok:
            copied_main = True

    if copied_main and tmp_path.is_file() and tmp_path.stat().st_size > 0:
        return tmp_path

    try:
        tmp_path.unlink(missing_ok=True)
    except Exception:
        pass
    return None


def _query_cookies_db(db: Path, sql: str, params: tuple = ()) -> list[tuple]:
    tmp = _copy_cookies_db_for_read(db)
    paths = [tmp, db] if tmp else [db]
    for path in paths:
        if not path or not path.is_file():
            continue
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            rows = conn.execute(sql, params).fetchall()
            conn.close()
            if tmp and path == tmp:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
            return rows
        except Exception:
            if tmp and path == tmp:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
            continue
    return []


def read_profile_cookies(profile_dir: Path, *, host: str = _SITE_HOST) -> dict[str, str]:
    """Đọc cookie từ profile Chrome (Chrome có thể đang mở)."""
    db = _cookies_db_path(profile_dir)
    if not db:
        return {}
    key = _chrome_aes_key(profile_dir)
    likes = _cookie_host_likes(host)
    placeholders = " OR ".join("host_key LIKE ?" for _ in likes)
    rows = _query_cookies_db(
        db,
        f"SELECT name, value, encrypted_value, host_key FROM cookies WHERE {placeholders}",
        likes,
    )
    out: dict[str, str] = {}
    for name, value, enc, _hk in rows:
        val = str(value or "").strip()
        if not val and enc:
            val = _decrypt_cookie_value(bytes(enc), key)
        if val:
            out[str(name)] = val
    return out


def profile_cookies_readable(profile_dir: Path) -> bool:
    """True nếu đọc được file Cookies (kể cả Chrome đang mở)."""
    db = _cookies_db_path(profile_dir)
    if not db:
        return False
    tmp = _copy_cookies_db_for_read(db)
    if not tmp:
        return False
    try:
        conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        n = conn.execute("SELECT COUNT(*) FROM cookies").fetchone()[0]
        conn.close()
        return int(n or 0) >= 0
    except Exception:
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm", "-journal"):
                p = Path(str(tmp) + suffix)
                p.unlink(missing_ok=True)
        except Exception:
            pass


def profile_has_cf_clearance(profile_dir: Path, *, host: str = _SITE_HOST) -> bool:
    db = _cookies_db_path(profile_dir)
    if not db:
        return False
    likes = _cookie_host_likes(host)
    placeholders = " OR ".join("host_key LIKE ?" for _ in likes)
    rows = _query_cookies_db(
        db,
        f"SELECT 1 FROM cookies WHERE name = 'cf_clearance' AND ({placeholders}) LIMIT 1",
        likes,
    )
    return bool(rows)


def launch_cms_chrome(
    profile_dir: Path,
    proxy: str,
    *,
    urls: list[str] | None = None,
    cdp_port: int = 0,
) -> subprocess.Popen[Any]:
    """
    Mở Chrome y hệt CMS (cms_launch.py).
    cdp_port=0 → không remote-debugging (CF ít chặn).
    """
    _browser_isolate_dir()
    from launcher import _launch_chrome_native  # type: ignore

    profile_dir.mkdir(parents=True, exist_ok=True)
    open_urls = urls or [f"{BASE_URL}/home/"]
    return _launch_chrome_native(
        profile_path=profile_dir,
        proxy=proxy,
        url=open_urls[0],
        urls=open_urls,
        cdp_port=int(cdp_port or 0),
    )


def wait_cf_clearance_in_profile(
    profile_dir: Path,
    *,
    timeout_sec: int = 180,
    host: str = _SITE_HOST,
    hint: str = "",
) -> dict[str, Any]:
    """Poll cookie profile — Chrome có thể đang mở (CMS hoặc script vừa launch)."""
    msg = hint or (
        "Nếu thấy trang chọn con vật Cloudflare, bấm ảnh + Xác nhận trong cửa sổ Chrome."
    )
    print(f"[REGISTER] {msg}", flush=True)
    deadline = time.time() + max(10, int(timeout_sec))
    last_hint = 0.0
    while time.time() < deadline:
        if profile_has_cf_clearance(profile_dir, host=host):
            return {"ok": True, "method": "profile_cookie"}
        if time.time() - last_hint > 20:
            print("[REGISTER] Đang chờ Cloudflare / cf_clearance trong profile…", flush=True)
            last_hint = time.time()
        time.sleep(2)
    return {"ok": False, "error": "cf_clearance_timeout"}


def wait_cf_clearance_profile(
    profile_dir: Path,
    proc: subprocess.Popen[Any],
    *,
    timeout_sec: int = 180,
    host: str = _SITE_HOST,
) -> dict[str, Any]:
    """Chờ cf_clearance xuất hiện trong profile (Chrome đang mở, không CDP)."""
    print(
        "[REGISTER] Chrome CMS (không CDP) — nếu thấy trang chọn con vật, "
        "bấm ảnh + Xác nhận trong cửa sổ Chrome.",
        flush=True,
    )
    deadline = time.time() + max(10, int(timeout_sec))
    last_hint = 0.0
    chrome_exited = False
    while time.time() < deadline:
        if proc.poll() is not None and not chrome_exited:
            chrome_exited = True
            if profile_has_cf_clearance(profile_dir, host=host):
                return {
                    "ok": True,
                    "method": "profile_cookie",
                    "url": "chrome_exited",
                    "exit": proc.returncode,
                }
            print(
                "[REGISTER] Chrome đóng sớm (profile có thể đang mở trong CMS) — "
                "tiếp tục chờ cf_clearance trong profile…",
                flush=True,
            )
        if profile_has_cf_clearance(profile_dir, host=host):
            return {"ok": True, "method": "profile_cookie"}
        if time.time() - last_hint > 20:
            print("[REGISTER] Đang chờ Cloudflare / cf_clearance trong profile…", flush=True)
            last_hint = time.time()
        time.sleep(2)
    return {"ok": False, "error": "cf_clearance_timeout"}


def terminate_chrome(proc: subprocess.Popen[Any] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def terminate_chrome_profile(profile_dir: Path) -> int:
    """Đóng mọi chrome.exe đang dùng user-data-dir của profile CMS."""
    if sys.platform != "win32":
        return 0
    marker = str(profile_dir.resolve()).lower().replace("'", "''")
    ps = (
        f"$marker = '{marker}'\n"
        "$n = 0\n"
        "Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
        "Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains($marker) } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $n++ }\n"
        "Write-Output $n\n"
    )
    killed = 0
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=30,
        )
        line = (r.stdout or "").strip().splitlines()[-1:] or ["0"]
        killed = int(line[0] or 0)
    except Exception:
        killed = 0
    if killed:
        time.sleep(1.5)
    return killed


def read_profile_cookies_after_close(
    session: dict,
    profile_dir: Path,
    *,
    host: str = _SITE_HOST,
    allow_kill: bool = False,
) -> dict[str, Any]:
    """Đọc cookie từ profile sau khi user login trong Chrome — giữ PHPSESSID."""
    loaded = warm_session_from_profile(
        session, profile_dir, host=host, allow_identity=True
    )
    if loaded.get("cookie_names"):
        return {**loaded, "restarted_chrome": False}

    if profile_cookies_readable(profile_dir):
        loaded = warm_session_from_profile(
            session, profile_dir, host=host, allow_identity=True
        )
        if loaded.get("cookie_names"):
            return {**loaded, "restarted_chrome": False}

    if profile_is_locked(profile_dir) and not allow_kill:
        return {
            **loaded,
            "restarted_chrome": False,
            "chrome_open": True,
            "cookies_db_locked": not bool(loaded.get("cookie_names")),
        }

    n = terminate_chrome_profile(profile_dir)
    wait_profile_unlocked(profile_dir, timeout_sec=20)
    time.sleep(1.0)
    loaded = warm_session_from_profile(
        session, profile_dir, host=host, allow_identity=True
    )
    return {**loaded, "restarted_chrome": bool(n), "terminated_chrome": n}


def warm_session_from_profile(
    session: dict,
    profile_dir: Path,
    *,
    host: str = _SITE_HOST,
    allow_identity: bool = False,
) -> dict[str, Any]:
    """
    Nạp cookie từ profile Chrome vào session (sau warm / đóng Chrome).

    Mặc định allow_identity=False — không lấy PHPSESSID (tránh lẫn phiên user
    khi chỉ warm CF). Sync sau login Chrome: allow_identity=True.
    """
    from xoso66_session import merge_session_cookies

    cookies = read_profile_cookies(profile_dir, host=host)
    merge_session_cookies(session, cookies, allow_identity=allow_identity)
    has_clearance = bool(cookies.get("cf_clearance"))
    return {
        "ok": has_clearance,
        "has_clearance": has_clearance,
        "cookie_names": sorted(cookies.keys()),
        "allow_identity": allow_identity,
    }


def profile_is_locked(profile_dir: Path) -> bool:
    """Chrome đang giữ profile (Singleton*)."""
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        if (profile_dir / name).exists():
            return True
    return False


def wait_profile_unlocked(profile_dir: Path, *, timeout_sec: int = 30) -> bool:
    """Chờ Chrome đóng profile (Singleton* biến mất)."""
    deadline = time.time() + max(3, int(timeout_sec))
    while time.time() < deadline:
        if not profile_is_locked(profile_dir):
            return True
        time.sleep(1)
    return not profile_is_locked(profile_dir)


def touch_cms_profile_session(
    session: dict,
    profile_dir: Path,
    *,
    dwell_sec: int = 15,
    timeout_sec: int = 120,
) -> dict[str, Any]:
    """
    Mở Chrome CMS (không CDP) vài giây để refresh CF cookie trước Playwright.
    Playwright persistent có thể làm cf_clearance cũ → /__verify/check.
    """
    proxy = str(session.get("proxy") or "").strip()
    if not proxy:
        return {"ok": False, "error": "missing_proxy"}
    to = max(5, int(dwell_sec))
    proc = launch_cms_chrome(profile_dir, proxy, cdp_port=0)
    meta: dict[str, Any] = {"touched": True, "pid": proc.pid, "dwell_sec": to}
    try:
        if not profile_has_cf_clearance(profile_dir):
            wait = wait_cf_clearance_profile(profile_dir, proc, timeout_sec=timeout_sec)
            meta["wait"] = wait
            if not wait.get("ok"):
                return {**meta, "ok": False, "error": wait.get("error") or "cf_clearance_timeout"}
        else:
            deadline = time.time() + to
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                time.sleep(1)
    finally:
        terminate_chrome(proc)
        time.sleep(1.0)
    loaded = warm_session_from_profile(session, profile_dir)
    meta.update(loaded)
    return meta


def cms_chrome_warm_session(
    session: dict,
    profile_dir: Path,
    *,
    timeout_sec: int | None = None,
    cms_device: str = "",
) -> dict[str, Any]:
    """
    Luồng anti-bot: mở Chrome như CMS → chờ CF → đóng → đọc cookie.
    Không Playwright, không CDP (mặc định).
    """
    proxy = str(session.get("proxy") or "").strip()
    if not proxy:
        return {"ok": False, "error": "missing_proxy"}
    dev = str(cms_device or os.environ.get("XOSO66_CMS_DEVICE") or "").strip()
    to = int(timeout_sec or os.environ.get("XOSO66_CF_MANUAL_WAIT_SEC", "180"))

    if profile_has_cf_clearance(profile_dir):
        meta = warm_session_from_profile(session, profile_dir)
        meta["skipped_launch"] = True
        if dev:
            meta["cms_device"] = dev
        return meta

    locked = profile_is_locked(profile_dir)
    if locked:
        print(
            f"[REGISTER] Profile CMS {dev or profile_dir.name} đang mở — "
            "chờ cf_clearance (giải captcha con vật trong Chrome nếu có)…",
            flush=True,
        )
        wait = wait_cf_clearance_in_profile(
            profile_dir,
            timeout_sec=to,
            hint="Chrome CMS đang mở — giải captcha Cloudflare trong cửa sổ đó.",
        )
        meta: dict[str, Any] = {
            "profile_locked": True,
            "skipped_launch": True,
            "wait": wait,
            "cms_device": dev or None,
        }
        if not wait.get("ok"):
            return {**meta, "ok": False, "error": wait.get("error") or "cf_clearance_timeout"}
        loaded = warm_session_from_profile(session, profile_dir)
        meta.update(loaded)
        return meta

    proc = launch_cms_chrome(profile_dir, proxy, cdp_port=0)
    meta = {"launched": True, "pid": proc.pid, "cdp": False, "cms_device": dev or None}
    try:
        wait = wait_cf_clearance_profile(profile_dir, proc, timeout_sec=to)
        meta["wait"] = wait
        if not wait.get("ok"):
            if profile_has_cf_clearance(profile_dir):
                wait = {"ok": True, "method": "profile_cookie_after_close"}
                meta["wait"] = wait
            else:
                return {**meta, "ok": False, "error": wait.get("error") or "cf_clearance_timeout"}
    finally:
        terminate_chrome(proc)
        time.sleep(1.0)

    loaded = warm_session_from_profile(session, profile_dir)
    meta.update(loaded)
    return meta
