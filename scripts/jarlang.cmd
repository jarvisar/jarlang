@echo off
rem Run JarLang from .venv, e.g. scripts\jarlang.cmd examples\hello.jlang
setlocal
set root=%~dp0..
if not exist "%root%\.venv\Scripts\python.exe" call "%~dp0setup.cmd" --no-pause || exit /b 1
"%root%\.venv\Scripts\python" -m jarlang %*
