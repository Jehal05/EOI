@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
.venv\Scripts\python tools\stack.py status
pause
