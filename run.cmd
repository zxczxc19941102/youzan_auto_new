@echo off
setlocal
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
  echo ???????????? setup.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
  if errorlevel 1 exit /b %errorlevel%
)
venv\Scripts\python.exe main.py %*
exit /b %errorlevel%
