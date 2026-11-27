@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 exit /b 1
if /i "%~1"=="--date" goto chingwin
if /i "%~1"=="--inbox" goto chingwin
if not exist "app\history_cli.py" goto chingwin
python -B -m app.history_cli %*
goto done

:chingwin
python -B -m app.chingwin_local_import %*

:done
set "exit_code=%errorlevel%"
if not "%exit_code%"=="0" echo AniShelf book update failed with exit code %exit_code%.
if "%~1"=="" pause
exit /b %exit_code%
