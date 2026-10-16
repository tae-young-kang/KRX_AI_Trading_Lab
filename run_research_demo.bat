@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (set "PY=py") else (set "PY=python")
%PY% research_cli.py --source DEMO_SYNTHETIC --topk 5
pause
