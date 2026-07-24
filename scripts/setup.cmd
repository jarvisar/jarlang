@echo off
rem Create .venv and install JarLang with the development tools
setlocal
cd /d "%~dp0.."

if exist .venv\Scripts\python.exe goto install
echo Creating .venv
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -m venv .venv
) else (
  python -m venv .venv
)
if errorlevel 1 goto failed

:install
echo Installing JarLang
.venv\Scripts\python -m pip install --upgrade pip --quiet
.venv\Scripts\python -m pip install -e ".[dev]"
if errorlevel 1 goto failed

echo.
echo Setup done.
echo   scripts\jarlang.cmd       start JarLang
echo   scripts\playground.cmd    open the playground
echo   scripts\test.cmd          run the tests
goto done

:failed
echo Setup failed. JarLang needs Python 3.10 or newer: https://www.python.org/downloads/
set failed=1

:done
rem Keep the window open when the script is double-clicked
if /i not "%~1"=="--no-pause" pause
if defined failed exit /b 1
exit /b 0
