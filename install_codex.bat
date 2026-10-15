@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install_codex.ps1" %*
exit /b %ERRORLEVEL%
