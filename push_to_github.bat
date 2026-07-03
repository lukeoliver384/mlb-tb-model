@echo off
REM Double-click to back up all your changes to GitHub. No typing needed.
cd /d "%~dp0"

echo ============================================================
echo  Uploading your latest work to GitHub...
echo ============================================================
echo.

git rev-parse --is-inside-work-tree >nul 2>nul
if errorlevel 1 (
  echo This folder isn't a git repo yet, or git isn't installed.
  echo Tell Claude and he'll help you set it up.
  echo.
  pause
  exit /b
)

echo Files that will be uploaded:
git add -A
git status --short
echo.

git commit -m "Add backtest, odds scraper, FastAPI backend, and web board UI"
if errorlevel 1 echo (Nothing new to commit -- everything is already backed up.)
echo.

echo Pushing to GitHub...
git push
echo.

echo ------------------------------------------------------------
echo  DONE. Check your repo at:
echo    https://github.com/lukeoliver384/mlb-tb-model
echo ------------------------------------------------------------
pause
