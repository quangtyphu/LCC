@echo off
chcp 65001 >nul
cd /d "%~dp0"
python benbet_auto_bet.py %*
