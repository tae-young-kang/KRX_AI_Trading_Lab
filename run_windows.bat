@echo off
cd /d "%~dp0"
call "%~dp0install_windows.bat"
if errorlevel 1 (
 pause
 exit /b 1
)
%PY% main.py
if errorlevel 1 pause
