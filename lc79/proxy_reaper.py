# -*- coding: utf-8 -*-
"""
Watchdog dọn tiến trình ``pproxy`` (relay SOCKS5->HTTP cho Chrome/Playwright) bị rò rỉ.

Bối cảnh: các module browser-automation (browser_isolate / c168 / xoso66 / allgame)
spawn pproxy làm cầu nối proxy cho Chrome. Khi Chrome đóng, pproxy thường bị bỏ lại
(mồ côi) và không ai dọn -> tích luỹ hàng trăm tiến trình, mỗi cái chiếm 1 cổng
ephemeral + giữ kết nối upstream. Đủ nhiều sẽ làm cạn cổng ephemeral toàn máy
(WinError 10048) khiến CẢ những tiến trình khác (vd. lc79 gọi 127.0.0.1:3000) chết theo.

Cách dọn an toàn: chỉ kill pproxy KHÔNG có kết nối Chrome nào đang cắm vào (idle).
pproxy đang phục vụ Chrome sống sẽ có ESTABLISHED 127.0.0.1 -> được giữ lại.
Để tránh giết nhầm Chrome chỉ tạm im, reaper nền yêu cầu idle qua nhiều lần quét
liên tiếp (``grace``) mới kill.

Chỉ dùng thư viện chuẩn + PowerShell (Get-NetTCPConnection / Get-CimInstance),
không cần psutil.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from collections import defaultdict

if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# PowerShell nhẹ: chỉ lấy pid + parent của các tiến trình pproxy (CommandLine chứa 'pproxy').
# Việc đếm kết nối làm bằng netstat trong Python (nhanh hơn Get-NetTCPConnection nhiều).
_PS_PIDS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$pp = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*pproxy*' } |
    Select-Object ProcessId, ParentProcessId
if (-not $pp) { '[]'; exit }
$alive = @((Get-Process).Id)
$out = foreach ($p in $pp) {
    [pscustomobject]@{
        pid    = [int]$p.ProcessId
        orphan = (-not ($alive -contains $p.ParentProcessId))
    }
}
$out | ConvertTo-Json -Compress
"""


def _run_ps(script: str, timeout: int = 60) -> str:
    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.stdout or ""


def _pproxy_pids() -> dict[int, bool]:
    """Trả {pid: orphan} cho các tiến trình pproxy đang chạy."""
    if sys.platform != "win32":
        return {}
    try:
        raw = _run_ps(_PS_PIDS).strip()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    if isinstance(data, dict):
        data = [data]
    out: dict[int, bool] = {}
    for d in data:
        if isinstance(d, dict) and "pid" in d:
            out[int(d["pid"])] = bool(d.get("orphan"))
    return out


def _netstat_ports() -> tuple[dict[int, set[int]], dict[int, set[int]]]:
    """
    Parse netstat -ano. Trả (listen, estab):
      listen[pid] = tập cổng 127.0.0.1 đang LISTENING của pid
      estab[pid]  = tập cổng local 127.0.0.1 đang ESTABLISHED của pid
    """
    listen: dict[int, set[int]] = defaultdict(set)
    estab: dict[int, set[int]] = defaultdict(set)
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except Exception:
        return listen, estab
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or not parts[0].upper().startswith("TCP"):
            continue
        local, _foreign, state, pid_s = parts[1], parts[2], parts[3], parts[-1]
        if "127.0.0.1:" not in local:
            continue
        try:
            pid = int(pid_s)
            port = int(local.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            continue
        if state.upper() == "LISTENING":
            listen[pid].add(port)
        elif state.upper() == "ESTABLISHED":
            estab[pid].add(port)
    return listen, estab


def scan() -> list[dict]:
    """Trả danh sách pproxy: [{pid, orphan, busy, ports}, ...].

    busy = pproxy có kết nối Chrome đang cắm vào (ESTABLISHED 127.0.0.1 trên đúng
    cổng nó đang LISTEN) -> đang phục vụ, KHÔNG được kill.
    """
    pids = _pproxy_pids()
    if not pids:
        return []
    listen, estab = _netstat_ports()
    result: list[dict] = []
    for pid, orphan in pids.items():
        lports = listen.get(pid, set())
        busy = bool(lports & estab.get(pid, set()))
        result.append(
            {"pid": pid, "orphan": orphan, "busy": busy, "ports": len(lports)}
        )
    return result


def _kill(pid: int) -> bool:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return True
    except Exception:
        return False


def cleanup_now(*, log: bool = True) -> dict:
    """
    Dọn NGAY: kill mọi pproxy idle (không có Chrome đang cắm). Giữ cái đang bận.
    Trả {total, killed, kept_busy}.
    """
    relays = scan()
    killed = 0
    kept = 0
    for r in relays:
        if r.get("busy"):
            kept += 1
            continue
        if _kill(int(r["pid"])):
            killed += 1
    result = {"total": len(relays), "killed": killed, "kept_busy": kept}
    if log:
        print(
            f"[PROXY_REAPER] Dọn ngay: tổng {result['total']} pproxy, "
            f"kill {killed} (idle), giữ {kept} (đang phục vụ Chrome).",
            flush=True,
        )
    return result


def _reaper_loop(interval: int, grace: int) -> None:
    # pid -> số lần quét liên tiếp đang idle
    idle_counts: dict[int, int] = {}
    while True:
        try:
            relays = scan()
            present = set()
            for r in relays:
                pid = int(r["pid"])
                present.add(pid)
                if r.get("busy"):
                    idle_counts[pid] = 0
                    continue
                idle_counts[pid] = idle_counts.get(pid, 0) + 1
                if idle_counts[pid] >= grace:
                    if _kill(pid):
                        print(
                            f"[PROXY_REAPER] Kill pproxy idle pid={pid} "
                            f"(idle {idle_counts[pid]} lần, orphan={r.get('orphan')}).",
                            flush=True,
                        )
                    idle_counts.pop(pid, None)
            # Quên các pid đã biến mất
            for pid in list(idle_counts):
                if pid not in present:
                    idle_counts.pop(pid, None)
        except Exception as e:
            print(f"[PROXY_REAPER] Lỗi vòng quét: {e}", flush=True)
        time.sleep(interval)


def start_proxy_reaper(interval_seconds: int = 90, grace_scans: int = 3) -> None:
    """
    Khởi động watchdog nền: mỗi ``interval_seconds`` quét pproxy, kill cái idle
    liên tục ``grace_scans`` lần (mặc định ~4.5 phút) -> tránh giết nhầm Chrome
    đang mở nhưng tạm không truyền dữ liệu.
    """
    if sys.platform != "win32":
        return
    t = threading.Thread(
        target=_reaper_loop,
        args=(interval_seconds, grace_scans),
        daemon=True,
        name="proxy-reaper",
    )
    t.start()
    print(
        f"[PROXY_REAPER] Đã bật watchdog pproxy (mỗi {interval_seconds}s, "
        f"kill sau {grace_scans} lần idle liên tiếp).",
        flush=True,
    )


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Dọn tiến trình pproxy rò rỉ.")
    ap.add_argument("--once", action="store_true", help="Dọn ngay 1 lần rồi thoát.")
    ap.add_argument("--list", action="store_true", help="Chỉ liệt kê, không kill.")
    ap.add_argument("--loop", action="store_true", help="Chạy watchdog liên tục.")
    ap.add_argument("--interval", type=int, default=90)
    ap.add_argument("--grace", type=int, default=3)
    args = ap.parse_args()

    if args.list:
        relays = scan()
        busy = sum(1 for r in relays if r.get("busy"))
        orphan = sum(1 for r in relays if r.get("orphan"))
        print(
            f"pproxy tổng={len(relays)} | busy={busy} | idle={len(relays) - busy} "
            f"| orphan={orphan}"
        )
        for r in relays:
            print(
                f"  pid={r['pid']:<7} busy={str(r.get('busy')):<5} "
                f"orphan={str(r.get('orphan')):<5} ports={r.get('ports')}"
            )
    elif args.loop:
        start_proxy_reaper(args.interval, args.grace)
        while True:
            time.sleep(3600)
    else:  # mặc định --once
        cleanup_now()
