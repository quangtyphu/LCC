@echo off
cd /d "%~dp0"
title C168 Chrome debug

set PROFILE=%TEMP%\c168-chrome-profile
set PORT=9222
set CHROME=

if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"

echo.
echo === C168 Chrome debug port %PORT% ===
echo.

if not defined CHROME (
  echo LOI: Khong tim thay chrome.exe
  pause
  exit /b 1
)

echo Chrome: %CHROME%
echo Dong het Chrome cu trong Task Manager neu port loi.
echo.
start "C168-Chrome" "%CHROME%" --remote-debugging-port=%PORT% --user-data-dir="%PROFILE%" --no-first-run "https://c168b2.cc/home/register"

timeout /t 4 /nobreak >nul
echo Mo trinh duyet: http://127.0.0.1:%PORT%/json/version de kiem tra
echo.
echo Tiep theo:
echo   python c168_register.py --manual --cdp http://127.0.0.1:%PORT%
echo.
pause
