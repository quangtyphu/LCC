@echo off
cd /d "%~dp0"
title C168 - Dang ky headless (khong mo cua so Chrome)
set PYTHONIOENCODING=utf-8
echo.
echo Chromium chay nen — khong hien cua so Chrome.
echo Neu loi 1134, thu lai hoac dung CHAY.bat (Chrome that).
echo.
python c168_register.py --click-only --random --headless
echo.
pause
