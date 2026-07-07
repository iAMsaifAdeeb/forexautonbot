@echo off
:: Gold Genious - bootstrap without PowerShell scripts (double-click or run from cmd)
title Gold Genious Bootstrap
cd /d "%~dp0"

echo.
echo  GOLD GENIOUS - Bootstrap
echo  ========================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0BOOTSTRAP.ps1"
if errorlevel 1 (
  echo.
  echo Bootstrap failed. Try running as Administrator.
  pause
)
