@echo off
rem Prints the password and the current one-time login code for a demo user (default: alice).
cd /d "%~dp0"
set PYTHONUTF8=1
if "%1"=="" (.venv\Scripts\python tools\stack.py code alice) else (.venv\Scripts\python tools\stack.py code %1)
pause
