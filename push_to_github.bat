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
if errorlevel 1 echo (Nothing new to commit -- your latest edits are already saved locally.)
echo.

echo Syncing with GitHub (pulling any remote changes first)...
git pull --no-rebase --no-edit
if errorlevel 1 (
  echo.
  echo *** Couldn't auto-merge -- there may be a conflict. ***
  echo Don't worry, nothing is lost. Send Claude this window's text and he'll fix it.
  echo.
  pause
  exit /b
)
echo.

echo Pushing to GitHub...
git push
if errorlevel 1 (
  echo.
  echo *** Push still failed. Copy this window's text to Claude. ***
) else (
  echo.
  echo ------------------------------------------------------------
  echo  DONE. Your work is backed up at:
  echo    https://github.com/lukeoliver384/mlb-tb-model
  echo ------------------------------------------------------------
)
pause
