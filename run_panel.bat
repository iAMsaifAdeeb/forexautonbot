@echo off
title Gold Genious
cd /d "%~dp0"

set "PY="
set "PYW="
where python >nul 2>&1 && (set "PY=python" & set "PYW=pythonw")
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
  set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  set "PYW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
)
if not defined PY if exist "C:\Program Files\Python312\python.exe" (
  set "PY=C:\Program Files\Python312\python.exe"
  set "PYW=C:\Program Files\Python312\pythonw.exe"
)
if not defined PY if exist "C:\Program Files\Python313\python.exe" (
  set "PY=C:\Program Files\Python313\python.exe"
  set "PYW=C:\Program Files\Python313\pythonw.exe"
)
if not defined PY (
  echo Python not found. Run SETUP.bat first.
  pause
  exit /b 1
)

"%PY%" -c "import sys; exit(0 if sys.version_info>=(3,13) else 1)" >nul 2>&1
if errorlevel 1 (
  "%PY%" -m pip install MetaTrader5 pandas "numpy>=1.26,<2" --only-binary :all: -q
) else (
  "%PY%" -m pip install MetaTrader5 pandas "numpy>=2.0" --only-binary :all: -q
)

if exist "%PYW%" (
  start "" "%PYW%" "%~dp0control_panel.py"
) else (
  "%PY%" "%~dp0control_panel.py"
)
