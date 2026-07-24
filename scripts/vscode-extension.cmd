@echo off
rem Build the VS Code extension and install it (needs Node.js)
setlocal
cd /d "%~dp0..\editors\vscode"

where npm >nul 2>nul
if errorlevel 1 (
  echo npm was not found. Install Node.js from https://nodejs.org/
  exit /b 1
)
call npm install || exit /b 1
call npm run package || exit /b 1

where code >nul 2>nul
if errorlevel 1 (
  echo Built editors\vscode\jarlang.vsix. Install it from VS Code: Extensions, then "Install from VSIX".
  exit /b 0
)
call code --install-extension jarlang.vsix --force || exit /b 1
echo Installed. Reload VS Code to use it.
