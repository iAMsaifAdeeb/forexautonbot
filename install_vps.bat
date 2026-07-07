@echo off
title Gold Genious - VPS Install
cd /d "%~dp0"

echo.
echo ========================================
echo   GOLD GENIOUS - VPS Setup
echo ========================================
echo.

:: --- Python find karo ---
set PY=python
where python >nul 2>&1
if errorlevel 1 (
    set PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
)
if not exist "%PY%" (
    set PY=C:\Program Files\Python313\python.exe
)
if not exist "%PY%" (
    echo ERROR: Python not found.
    echo Install from https://www.python.org/downloads/
    echo Tick "Add Python to PATH" during install.
    pause
    exit /b 1
)
echo Using Python: %PY%

:: --- GitHub se download ---
echo.
echo [1/4] Downloading from GitHub...
powershell -NoProfile -Command ^
  "try { Invoke-WebRequest -Uri 'https://github.com/iAMsaifAdeeb/forexautonbot/archive/refs/heads/main.zip' -OutFile 'repo.zip' -UseBasicParsing; exit 0 } catch { Write-Host $_.Exception.Message; exit 1 }"
if errorlevel 1 (
    echo ERROR: Download failed. Check internet connection.
    pause
    exit /b 1
)

:: --- Extract ---
echo [2/4] Extracting files...
powershell -NoProfile -Command "Expand-Archive -Path 'repo.zip' -DestinationPath '.' -Force"
if not exist "forexautonbot-main" (
    echo ERROR: Extract failed.
    pause
    exit /b 1
)
xcopy /E /Y /I "forexautonbot-main\*" "." >nul
rmdir /S /Q "forexautonbot-main" 2>nul
del /Q "repo.zip" 2>nul
echo Files installed to: %CD%

:: --- Dependencies ---
echo.
echo [3/4] Installing Python packages...
"%PY%" -m pip install --upgrade pip --quiet
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: pip install failed.
    pause
    exit /b 1
)

:: --- Done ---
echo.
echo [4/4] Setup complete!
echo.
echo Next steps:
echo   1. Open MetaTrader 5 and login
echo   2. Enable Algo Trading in MT5
echo   3. Add XAUUSD to Market Watch
echo   4. Run:  run_panel.bat
echo      Or:   python control_panel.py
echo.
pause
