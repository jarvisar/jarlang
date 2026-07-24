@echo off
rem Run the tests
setlocal
cd /d "%~dp0.."
if not exist .venv\Scripts\python.exe call "%~dp0setup.cmd" --no-pause || exit /b 1
.venv\Scripts\python -m pytest %*
