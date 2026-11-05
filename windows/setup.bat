@echo off
rem One-time setup: checks Java and Python, then installs the Python packages into a private folder (.venv).
cd /d "%~dp0"
set PYTHONUTF8=1
where java >nul 2>nul || (echo Java was not found. Install Java 21 or newer from https://adoptium.net then run this again. & pause & exit /b 1)
where py >nul 2>nul || (echo Python was not found. Install Python 3.12 or 3.13 from https://www.python.org/downloads/ and tick "Add python.exe to PATH". & pause & exit /b 1)
if not exist .venv (py -3 -m venv .venv || (echo Could not create the Python environment. & pause & exit /b 1))
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r services\ems-analytics\requirements.txt -r services\historian\requirements.txt || (echo Package install failed. Send a screenshot of this window. & pause & exit /b 1)
echo.
echo Setup finished. Now double-click start-demo.bat
pause
