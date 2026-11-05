@echo off
rem Starts the whole demo stack and opens the screen in your browser.
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist .venv\Scripts\python.exe (echo Run setup.bat first. & pause & exit /b 1)
.venv\Scripts\python tools\stack.py up
if errorlevel 1 (echo. & echo The stack did not start. Logs are in %TEMP%\gs. Send a screenshot of this window. & pause & exit /b 1)
.venv\Scripts\python tools\stack.py code alice
start "" http://127.0.0.1:8083
echo.
echo Keep this window open or close it: the demo keeps running. To stop, double-click stop-demo.bat
pause
