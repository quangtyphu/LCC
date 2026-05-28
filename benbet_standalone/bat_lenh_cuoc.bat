@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo === Mo Chrome + bat lenh WS khi dat cuoc tay ===
echo.
python benbet_capture_browser.py -u longmebaihai -p Valentine1 --fresh-chrome
pause
