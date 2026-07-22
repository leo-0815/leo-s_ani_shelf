@echo off
setlocal
cd /d "%~dp0"
python -m app.history_cli
set "exit_code=%errorlevel%"
if not "%exit_code%"=="0" echo AniShelf history job failed with exit code %exit_code%.
pause
exit /b %exit_code%
