@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo ARTEMIS is not installed. Run install_codex.bat first.
    exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -m scripts.run_artemis %*
exit /b %ERRORLEVEL%
