@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>&1
if %errorlevel%==0 (
  python -m pip install -r requirements.txt -q
  python app.py
) else (
  python3 -m pip install -r requirements.txt -q
  python3 app.py
)
if errorlevel 1 pause
