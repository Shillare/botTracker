@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
echo Starting bot... logs in bot.log
:loop
python -u main.py
echo [%date% %time%] python exited (code %errorlevel%), restart in 5s... >> auto_restart.log
timeout /t 5 /nobreak >nul
goto loop
