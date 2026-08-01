@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Doi mat khau login tat ca acc -> MotHaiBa4 (site khong nhan @)
echo API: POST /server/user/updatepassword
python xoso66_change_login_password.py --all -n MotHaiBa4 -j 8
pause
