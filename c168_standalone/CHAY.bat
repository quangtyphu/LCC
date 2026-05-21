@echo off
cd /d "%~dp0"
title C168 - Tu dong dang ky
set PYTHONIOENCODING=utf-8
echo.
echo Tu dong: dong Chrome cu, mo Chrome moi, dien form, bam DANG KY, luu tai khoan.
echo Ban khong can lam gi — chi xem ket qua trong CMD.
echo.
python c168_register.py --click-only --random
echo.
pause
