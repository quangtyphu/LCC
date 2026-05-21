@echo off
cd /d "%~dp0"
if not exist proxies.txt (
  echo Tao file proxies.txt — moi dong: host:port:user:pass
  copy /Y proxies.example.txt proxies.txt
)
python main.py new --ephemeral --proxy-file proxies.txt --url about:blank
pause
