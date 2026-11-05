@echo off
rem Demo step: simulates a breaker trip in the field. Then look at the Alarms screen.
cd /d "%~dp0"
set PYTHONUTF8=1
.venv\Scripts\python tools\stack.py trip
pause
