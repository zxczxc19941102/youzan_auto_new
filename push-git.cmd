@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0push-git.ps1" %*
exit /b %ERRORLEVEL%
