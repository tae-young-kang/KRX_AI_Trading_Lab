@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (set "PY=py") else (set "PY=python")
%PY% -m pip install -r requirements.txt pyinstaller
if errorlevel 1 exit /b 1
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --name KOSPI_AI_Trading --collect-all keyring main.py
if errorlevel 1 (pause & exit /b 1)
echo DONE: dist\KOSPI_AI_Trading.exe
pause
