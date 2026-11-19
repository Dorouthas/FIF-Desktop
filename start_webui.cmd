@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap.ps1" -Mode web -NoLaunch
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m fif_oral.web %*
if errorlevel 1 pause
