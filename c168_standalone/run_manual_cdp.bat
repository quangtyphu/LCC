@echo off
chcp 65001 >nul
cd /d "%~dp0"
title C168 - Manual + CDP (1 click)

echo Buoc 1: Mo Chrome debug...
call "%~dp0start_chrome_debug.bat"
if errorlevel 1 exit /b 1

echo.
echo Buoc 2: Gan script ghi log (ban tu dang ky tren Chrome)...
set PYTHONIOENCODING=utf-8
python c168_register.py --manual --cdp http://127.0.0.1:9222
pause
