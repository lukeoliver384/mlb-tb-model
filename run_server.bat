@echo off
REM Double-click this file to start the backend API, then open the /docs page.
cd /d "%~dp0"
set PY=py
where py >nul 2>nul || set PY=python

echo Installing backend requirements (first run only)...
%PY% -m pip install -r requirements-api.txt

echo.
echo ============================================================
echo  Starting your Prop Board at http://127.0.0.1:8000
echo  (API docs are at http://127.0.0.1:8000/docs)
echo  Leave this window open. Press Ctrl+C here to stop it.
echo ============================================================
start "" http://127.0.0.1:8000
%PY% -m uvicorn server:app --port 8000
pause
