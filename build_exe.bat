@echo off
title Building Control Panel EXE
cd /d "%~dp0"
set PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=python
"%PY%" -m pip install pyinstaller --quiet
"%PY%" -m PyInstaller --onefile --noconsole --name "XAUUSD Bot Control Panel" --distpath . control_panel.py
echo.
echo Done. Look for "XAUUSD Bot Control Panel.exe" in this folder.
pause
