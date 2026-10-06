@echo off
REM ============================================================
REM  MetaAdsPlaywright worker - runs continuously in the background.
REM  Double-click this file to start it, or let Task Scheduler run it
REM  automatically at logon.
REM ============================================================
cd /d "%~dp0"

REM Pull the latest worker code (safe if already up to date).
git pull

call venv\Scripts\activate.bat

:loop
echo [%date% %time%] starting worker...
python worker.py --url https://ads.botdz.com --token 8d65ff5ed68bdac55ae40926d14105c417d1da6291881ff5 --poll 10
echo [%date% %time%] worker exited - restarting in 15s...
timeout /t 15 /nobreak >nul
goto loop
