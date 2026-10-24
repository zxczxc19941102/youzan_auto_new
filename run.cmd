@echo off
setlocal
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
  echo ???????????? setup.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
  if errorlevel 1 exit /b %errorlevel%
)
venv\Scripts\python.exe main.py %*
<<<<<<< HEAD
exit /b %errorlevel%
=======
exit /b %errorlevel%
>>>>>>> b94e3e2 (修复Python 3.14登录并持久化本地配置)
