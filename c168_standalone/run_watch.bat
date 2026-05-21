@echo off
cd /d "%~dp0"
echo Dang ky C168: thu toi da 15 proxy SOCKS5 tu game_data.db ...
python c168_register.py --random --headed --keep-open --proxy-max 15
echo.
pause
