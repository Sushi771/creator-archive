@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0upgrade.ps1" %*
if errorlevel 1 pause
exit /b %errorlevel%
