@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (set "PY=py") else (set "PY=python")
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
 echo Dependency installation failed. Check Python and network access.
 exit /b 1
)
%PY% -m app.environment_check
if errorlevel 1 exit /b 1
echo Installation check complete. The program has not been started.
exit /b 0
