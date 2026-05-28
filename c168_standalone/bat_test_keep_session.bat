@echo off
REM Test nhanh: profile da login, Game B tab nen, roi cuoc Python (khong can tab vendor).
cd /d "%~dp0"
python -u c168_login_open_game.py -u giaitanhettvt -p Valentine1 --proxy "14.224.198.91:27624:paPXXV:oEvaJY" --auto-bet --headless-play --keep-session --skip-proxy-check
pause
