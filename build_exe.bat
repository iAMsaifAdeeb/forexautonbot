@echo off
title Building Control Panel EXE
cd /d "%~dp0"
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" -m PyInstaller --onefile --noconsole --name "XAUUSD Bot Control Panel" --distpath . control_panel.py
echo.
echo Done. Look for "XAUUSD Bot Control Panel.exe" in this folder.
pause
