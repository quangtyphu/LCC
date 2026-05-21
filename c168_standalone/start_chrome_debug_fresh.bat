@echo off
cd /d "%~dp0"
echo Dong Chrome va xoa profile C168...
taskkill /IM chrome.exe /F 2>nul
timeout /t 2 /nobreak >nul
if exist "%TEMP%\c168-chrome-profile" rd /s /q "%TEMP%\c168-chrome-profile"
call start_chrome_debug.bat
