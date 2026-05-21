#!/usr/bin/env python3
"""LC79 — chạy từ thư mục gốc repo: python main.py"""
from pathlib import Path
import os
import runpy
import sys

_LC79 = Path(__file__).resolve().parent / "lc79"
_entry = _LC79 / "main.py"
if not _entry.is_file():
    raise SystemExit(f"Không thấy {_entry}")

os.chdir(_LC79)
if str(_LC79) not in sys.path:
    sys.path.insert(0, str(_LC79))

runpy.run_path(str(_entry), run_name="__main__")
