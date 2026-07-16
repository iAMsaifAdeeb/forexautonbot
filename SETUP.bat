@echo off
setlocal EnableExtensions
title Gold Genious - Setup
cd /d "%~dp0"

echo.
echo  ============================================
echo    GOLD GENIOUS  -  Setup / Update
echo  ============================================
echo.

:: ---------- Find Python ----------
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
  echo  ERROR: Python not found.
  echo  Install Python 3.12 from https://www.python.org/downloads/
  echo  Tick "Add Python to PATH" then run this file again.
  pause
  exit /b 1
)
if not exist "%PYW%" set "PYW=%PY%"
echo  Python: %PY%

:: ---------- Download latest from GitHub ----------
echo.
echo  [1/5] Updating all files from GitHub...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$zip='repo.zip';" ^
  "Invoke-WebRequest -Uri 'https://github.com/iAMsaifAdeeb/forexautonbot/archive/refs/heads/main.zip' -OutFile $zip -UseBasicParsing;" ^
  "Expand-Archive -Path $zip -DestinationPath '.' -Force;" ^
  "Copy-Item 'forexautonbot-main\*' '.' -Recurse -Force;" ^
  "Remove-Item 'forexautonbot-main',$zip -Recurse -Force -ErrorAction SilentlyContinue"
if errorlevel 1 (
  echo  WARNING: GitHub download failed - using files already in this folder.
) else (
  echo  All files updated.
)
cd /d "%~dp0"

:: ---------- Kill / remove OLD frozen panel EXE (causes old UI) ----------
echo.
echo  [2/5] Removing old Gold Genious.exe (frozen UI cannot update)...
taskkill /F /IM "Gold Genious.exe" >nul 2>&1
taskkill /F /IM "XAUUSD Bot Control Panel.exe" >nul 2>&1
taskkill /F /IM "Gold Sniper.exe" >nul 2>&1
del /F /Q "Gold Genious.exe" >nul 2>&1
del /F /Q "XAUUSD Bot Control Panel.exe" >nul 2>&1
del /F /Q "Gold Sniper.exe" >nul 2>&1
if exist "%USERPROFILE%\Desktop\Gold Genious.exe" del /F /Q "%USERPROFILE%\Desktop\Gold Genious.exe" >nul 2>&1
echo  Old EXE cleared. Use Desktop shortcut (Python) only.

:: ---------- Install packages ----------
echo.
echo  [3/5] Installing packages...
"%PY%" -m pip install --upgrade pip --quiet
"%PY%" -c "import sys; exit(0 if sys.version_info>=(3,13) else 1)" >nul 2>&1
if errorlevel 1 (
  echo  Python 3.12 - installing numpy 1.x wheels...
  "%PY%" -m pip install MetaTrader5 pandas "numpy>=1.26,<2" --only-binary :all:
) else (
  echo  Python 3.13+ - installing numpy 2.x wheels...
  "%PY%" -m pip install MetaTrader5 pandas "numpy>=2.0" --only-binary :all:
)
if errorlevel 1 (
  echo  ERROR: Package install failed.
  pause
  exit /b 1
)
echo  Packages OK.

:: ---------- Desktop shortcut ----------
echo.
echo  [4/5] Creating Desktop icon...
set "BOTDIR=%CD%"
set "VBS=%BOTDIR%\run_panel.vbs"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Gold Genious.lnk');" ^
  "$s.TargetPath='wscript.exe';" ^
  "$s.Arguments='\"\"%VBS%\"\"';" ^
  "$s.WorkingDirectory='%BOTDIR%';" ^
  "$s.IconLocation=\"$env:SystemRoot\System32\imageres.dll,98\";" ^
  "$s.Description='Gold Genious XAUUSD Auto Trader';" ^
  "$s.Save()"
echo  Desktop icon created: Gold Genious

:: ---------- Done ----------
echo.
echo  [5/5] Setup complete!
echo.
echo  Double-click "Gold Genious" on your Desktop to start.
echo  Window title must say: Gold Genious — Strategies V28
echo  Flow: toggle ONE strategy ON → press big START (MT5 opens + bot runs).
echo.
echo  Before trading:
echo    - MetaTrader 5 open + logged in
echo    - Algo Trading ON  (your log shows it was OFF)
echo    - XAUUSD in Market Watch
echo.
set /p LAUNCH="Start Gold Genious now? (Y/N): "
if /i "%LAUNCH%"=="Y" (
  if exist "%PYW%" (start "" "%PYW%" "%BOTDIR%\control_panel.py") else (start "" "%PY%" "%BOTDIR%\control_panel.py")
)
exit /b 0
