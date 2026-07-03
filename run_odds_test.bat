@echo off
REM Double-click this file to test the odds scraper. No typing needed.
cd /d "%~dp0"
set PY=py
where py >nul 2>nul || set PY=python

echo ============================================================
echo  Dumping Bovada's raw payload (for parser tuning)...
echo ============================================================
%PY% odds_scrape.py --dump bovada_raw.json > odds_output.txt 2>&1

echo. >> odds_output.txt
echo ============================================================ >> odds_output.txt
echo  Matched board (dry run) >> odds_output.txt
echo ============================================================ >> odds_output.txt
%PY% odds_scrape.py --dry-run >> odds_output.txt 2>&1

type odds_output.txt
echo.
echo ------------------------------------------------------------
echo  DONE. Two files were saved in this folder:
echo    - bovada_raw.json     (drag this into the chat)
echo    - odds_output.txt     (drag this into the chat too)
echo ------------------------------------------------------------
pause
