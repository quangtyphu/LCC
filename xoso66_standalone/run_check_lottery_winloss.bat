@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Quet lich su cuoc Xo so (7 ngay) — song song 16 luong...
python xoso66_check_lottery_winloss.py --all --days 7 -j 16
echo.
echo CSV mac dinh: C:\Users\Quang\Documents\CMS\game_data\lottery_winloss_hits.csv
pause
