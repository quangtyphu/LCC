# -*- coding: utf-8 -*-
"""
Guardian gắn vòng đời 1 relay pproxy vào đúng tiến trình Chrome dùng nó.

Chrome capture/CMS mở detached -> relay pproxy dễ bị bỏ lại mồ côi -> cạn cổng
ephemeral. Guardian chạy detached, theo dõi PID Chrome; Chrome thoát thì kill relay
rồi tự thoát. Mỗi Chrome (có relay) => 1 guardian nhẹ, tự kết thúc cùng Chrome.

Chạy: pythonw relay_guardian.py --chrome-pid <P> --relay-pid <R>
Chỉ dùng thư viện chuẩn (ctypes) — không cần psutil.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time

_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0x0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            import os

            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes

    k = ctypes.windll.kernel32
    handle = k.OpenProcess(_SYNCHRONIZE, False, int(pid))
    if not handle:
        return False
    try:
        return k.WaitForSingleObject(handle, 0) != _WAIT_OBJECT_0
    finally:
        k.CloseHandle(handle)


def _kill(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F", "/T"],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass


def watch(chrome_pid: int, relay_pid: int, poll: float = 2.0) -> None:
    while _pid_alive(chrome_pid):
        if not _pid_alive(relay_pid):
            return
        time.sleep(poll)
    if _pid_alive(relay_pid):
        _kill(relay_pid)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chrome-pid", type=int, required=True)
    ap.add_argument("--relay-pid", type=int, required=True)
    ap.add_argument("--poll", type=float, default=2.0)
    args = ap.parse_args()
    watch(args.chrome_pid, args.relay_pid, poll=args.poll)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
