@echo off
REM Double-click to confirm every Python file compiles (no syntax errors)
REM before you run the app or push to GitHub.
cd /d "%~dp0"
set PY=py
where py >nul 2>nul || set PY=python

echo Checking all Python files compile cleanly...
echo.
%PY% -m py_compile app.py odds_scrape.py backtest.py server.py scripts\daily_ping.py data.py engine.py park_factors.py tracker.py

if errorlevel 1 (
  echo.
  echo *** A file has a SYNTAX ERROR -- copy the message above and send it to Claude. ***
  echo Do NOT push until this is fixed.
) else (
  echo.
  echo ------------------------------------------------------------
  echo  All files compile cleanly. Safe to run and to push.
  echo ------------------------------------------------------------
)
echo.
pause
