@echo off
title Building Gold Genious EXE (thin launcher)
cd /d "%~dp0"
set PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=python
"%PY%" -m pip install pyinstaller --quiet
"%PY%" -m PyInstaller --onefile --noconsole --name "Gold Genious" --distpath . ^
  launcher_stub.py
echo.
echo Done. "Gold Genious.exe" is a thin launcher that always opens control_panel.py
echo so Update can change the UI without rebuilding the EXE.
pause
