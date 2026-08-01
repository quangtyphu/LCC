# -*- coding: utf-8 -*-
"""Timestamp prefix + optional file tee cho mọi print()/stdout/stderr — lc79 + xoso66."""

from __future__ import annotations

import atexit
import builtins
import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO
from zoneinfo import ZoneInfo

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_TS_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} ")
_INSTALLED_ATTR = "_lc79_timed_print_installed"
_ORIG_PRINT_ATTR = "_lc79_orig_print"
_TEE_INSTALLED_ATTR = "_lc79_file_tee_installed"

_tee_lock = threading.Lock()
_tee_file: TextIO | None = None
_tee_day: str | None = None
_tee_dir: Path | None = None
_tee_prefix: str = "app"
_tee_path: Path | None = None


def log_timestamp() -> str:
    """Format: 2026-06-30 23:30:36,993 (Asia/Ho_Chi_Minh)."""
    now = datetime.now(_VN_TZ)
    return now.strftime("%Y-%m-%d %H:%M:%S") + f",{now.microsecond // 1000:03d}"


def _is_console_stream(file: Any) -> bool:
    if file is None:
        return True
    return file in (sys.stdout, sys.stderr)


def _prefix_message(text: str) -> str:
    if not text:
        return log_timestamp()
    if _TS_PREFIX_RE.match(text):
        return text
    if text.startswith("\n"):
        rest = text[1:]
        if rest and _TS_PREFIX_RE.match(rest):
            return text
        return "\n" + log_timestamp() + " " + rest
    return log_timestamp() + " " + text


def _timed_print(*args: object, **kwargs: object) -> None:
    orig: Any = getattr(builtins, _ORIG_PRINT_ATTR, builtins.print)
    file = kwargs.get("file")
    if not _is_console_stream(file):
        orig(*args, **kwargs)
        return

    sep = str(kwargs.get("sep") or " ")
    end = str(kwargs.get("end") or "\n")
    flush = bool(kwargs.get("flush"))
    out_file = file or sys.stdout

    if not args:
        prefixed = log_timestamp()
    else:
        text = sep.join(str(a) for a in args)
        prefixed = _prefix_message(text)

    try:
        orig(prefixed, end=end, file=out_file, flush=flush)
    except UnicodeEncodeError:
        buf = getattr(out_file, "buffer", None)
        payload = prefixed + end
        if buf is not None:
            buf.write(payload.encode("utf-8", errors="replace"))
            if flush:
                buf.flush()
        else:
            enc = getattr(out_file, "encoding", None) or "utf-8"
            out_file.write(payload.encode(enc, errors="replace").decode(enc, errors="replace"))
            if flush:
                out_file.flush()


def install_timed_print(*, enabled: bool | None = None) -> None:
    """Patch builtins.print — idempotent; LC79_LOG_NO_TS=1 để tắt."""
    if getattr(builtins, _INSTALLED_ATTR, False):
        return
    if enabled is None:
        enabled = os.environ.get("LC79_LOG_NO_TS", "").strip().lower() not in (
            "1",
            "true",
            "yes",
        )
    if not enabled:
        setattr(builtins, _INSTALLED_ATTR, True)
        return
    setattr(builtins, _ORIG_PRINT_ATTR, builtins.print)
    builtins.print = _timed_print  # type: ignore[assignment]
    setattr(builtins, _INSTALLED_ATTR, True)


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _close_tee_file() -> None:
    global _tee_file, _tee_day, _tee_path
    with _tee_lock:
        fp = _tee_file
        _tee_file = None
        _tee_day = None
        _tee_path = None
        if fp is not None:
            try:
                fp.flush()
            except Exception:
                pass
            try:
                fp.close()
            except Exception:
                pass


def _ensure_tee_file() -> TextIO | None:
    """Mở / xoay file theo ngày (Asia/Ho_Chi_Minh). Caller giữ _tee_lock."""
    global _tee_file, _tee_day, _tee_path
    if _tee_dir is None:
        return None
    day = datetime.now(_VN_TZ).strftime("%Y%m%d")
    if _tee_file is not None and _tee_day == day:
        return _tee_file
    if _tee_file is not None:
        try:
            _tee_file.flush()
            _tee_file.close()
        except Exception:
            pass
        _tee_file = None
    _tee_dir.mkdir(parents=True, exist_ok=True)
    path = _tee_dir / f"{_tee_prefix}_{day}.log"
    _tee_file = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
    _tee_day = day
    _tee_path = path
    return _tee_file


def _tee_write(text: str) -> None:
    if not text:
        return
    with _tee_lock:
        fp = _ensure_tee_file()
        if fp is None:
            return
        try:
            fp.write(text)
            fp.flush()
        except Exception:
            pass


class _TeeTextIO:
    """Ghi đồng thời ra console thật + file log."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def write(self, s: str) -> int:
        if not isinstance(s, str):
            s = str(s)
        n = self._stream.write(s)
        _tee_write(s)
        return n

    def writelines(self, lines: Any) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        self._stream.flush()
        with _tee_lock:
            if _tee_file is not None:
                try:
                    _tee_file.flush()
                except Exception:
                    pass

    def isatty(self) -> bool:
        try:
            return bool(self._stream.isatty())
        except Exception:
            return False

    @property
    def encoding(self) -> str | None:
        return getattr(self._stream, "encoding", None)

    @property
    def errors(self) -> str | None:
        return getattr(self._stream, "errors", None)

    @property
    def buffer(self) -> Any:
        return getattr(self._stream, "buffer", None)

    def fileno(self) -> int:
        return self._stream.fileno()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def current_log_path() -> Path | None:
    """Path file log đang mở (nếu đã install_file_tee)."""
    with _tee_lock:
        return _tee_path


def _purge_old_logs(log_dir: Path, prefix: str, keep_days: int) -> int:
    """Xóa ``{prefix}_YYYYMMDD.log`` cũ hơn keep_days (theo ngày VN). Trả số file đã xóa."""
    if keep_days < 1 or not log_dir.is_dir():
        return 0
    today = datetime.now(_VN_TZ).date()
    pat = re.compile(
        rf"^{re.escape(prefix)}_(\d{{8}})\.log$",
        re.IGNORECASE,
    )
    removed = 0
    try:
        entries = list(log_dir.iterdir())
    except OSError:
        return 0
    for p in entries:
        if not p.is_file():
            continue
        m = pat.match(p.name)
        if not m:
            continue
        try:
            file_day = datetime.strptime(m.group(1), "%Y%m%d").date()
        except ValueError:
            continue
        age = (today - file_day).days
        if age < keep_days:
            continue
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def install_file_tee(
    *,
    log_dir: str | Path | None = None,
    prefix: str = "xoso66",
    enabled: bool | None = None,
    keep_days: int | None = None,
    env_enable: str = "XOSO66_LOG_TO_FILE",
    env_dir: str = "XOSO66_LOG_DIR",
    env_keep_days: str = "XOSO66_LOG_KEEP_DAYS",
) -> Path | None:
    """
    Tee sys.stdout + sys.stderr → file theo ngày: ``{prefix}_YYYYMMDD.log``.

    - Mặc định bật (enabled=True nếu không set).
    - Tắt: ``XOSO66_LOG_TO_FILE=0`` (hoặc enabled=False).
    - Thư mục: ``XOSO66_LOG_DIR`` hoặc tham số log_dir.
    - Giữ log: mặc định 7 ngày; ``XOSO66_LOG_KEEP_DAYS`` hoặc keep_days.
    """
    global _tee_dir, _tee_prefix

    if getattr(builtins, _TEE_INSTALLED_ATTR, False):
        return current_log_path()

    if enabled is None:
        enabled = _env_truthy(env_enable, default=True)
    if not enabled:
        setattr(builtins, _TEE_INSTALLED_ATTR, True)
        return None

    override = (os.environ.get(env_dir) or "").strip()
    if override:
        _tee_dir = Path(override)
    elif log_dir is not None:
        _tee_dir = Path(log_dir)
    else:
        _tee_dir = Path.cwd() / "logs"
    _tee_prefix = str(prefix or "app").strip() or "app"

    if keep_days is None:
        raw_keep = (os.environ.get(env_keep_days) or "").strip()
        try:
            keep_days = int(raw_keep) if raw_keep else 7
        except ValueError:
            keep_days = 7
    keep_days = max(1, int(keep_days))

    with _tee_lock:
        fp = _ensure_tee_file()
        path = _tee_path

    if fp is None or path is None:
        setattr(builtins, _TEE_INSTALLED_ATTR, True)
        return None

    # Chỉ wrap một lần — giữ stream gốc bên trong Tee.
    if not isinstance(sys.stdout, _TeeTextIO):
        sys.stdout = _TeeTextIO(sys.stdout)  # type: ignore[assignment]
    if not isinstance(sys.stderr, _TeeTextIO):
        sys.stderr = _TeeTextIO(sys.stderr)  # type: ignore[assignment]

    atexit.register(_close_tee_file)
    setattr(builtins, _TEE_INSTALLED_ATTR, True)

    purged = _purge_old_logs(_tee_dir, _tee_prefix, keep_days)

    # Báo path — sau khi wrap nên dòng này cũng vào file.
    try:
        extra = f" | purge {purged} file(s) >={keep_days}d" if purged else f" | keep {keep_days}d"
        print(f"[LOG] Console → {path}{extra}", flush=True)
    except Exception:
        pass
    return path
