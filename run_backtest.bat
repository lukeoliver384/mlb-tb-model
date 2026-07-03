@echo off
REM Double-click this file to run the historical backtest. No typing needed.
cd /d "%~dp0"
set PY=py
where py >nul 2>nul || set PY=python

echo ============================================================
echo  Running point-in-time backtest (Apr 1 - Jun 30, 2026)...
echo  This makes many MLB API calls and can take a few minutes.
echo ============================================================
%PY% backtest.py --start 2026-04-01 --end 2026-06-30 > backtest_output.txt 2>&1

type backtest_output.txt
echo.
echo ------------------------------------------------------------
echo  DONE. Saved in this folder:
echo    - backtest_output.txt   (drag this into the chat)
echo    - backtest_results.csv  (raw graded legs)
echo ------------------------------------------------------------
pause
