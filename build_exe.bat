@echo off
title Building Gold Genious EXE
cd /d "%~dp0"
set PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=python
"%PY%" -m pip install pyinstaller "numpy<2" MetaTrader5 pandas --quiet
"%PY%" -m PyInstaller --onefile --noconsole --name "Gold Genious" --distpath . ^
  --collect-all numpy ^
  --collect-all MetaTrader5 ^
  --hidden-import=numpy._core._multiarray_umath ^
  --hidden-import=numpy._core.multiarray ^
  control_panel.py
echo.
echo Done. Look for "Gold Genious.exe" in this folder.
pause
