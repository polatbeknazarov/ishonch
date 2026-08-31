@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>&1
if %errorlevel%==0 (
  python app.py
) else (
  python3 app.py
)
if errorlevel 1 pause
