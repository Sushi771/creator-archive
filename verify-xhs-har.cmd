@echo off
setlocal
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
if "%~1"=="" (
  echo Drag Edge-exported sanitized HAR files onto this launcher.
  echo Guide: docs/07 - XHS HAR validation.
  pause
  exit /b 2
)
if not exist ".venv\Scripts\python.exe" (
  echo Missing local Python environment. See docs/07.
  pause
  exit /b 2
)
".venv\Scripts\python.exe" -X utf8 -m scripts.verify_xhs_har --har %*
set "HAR_VERIFY_RESULT=%ERRORLEVEL%"
pause
exit /b %HAR_VERIFY_RESULT%
