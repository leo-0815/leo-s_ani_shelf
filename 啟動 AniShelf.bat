@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title AniShelf Local Server

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python was not found in PATH.
  echo Install Python 3.10 or newer, then try again.
  echo.
  pause
  exit /b 1
)

python start_anishelf.py
if errorlevel 1 (
  echo.
  echo AniShelf failed to start. See anishelf.err.log for details.
  pause
)

endlocal
