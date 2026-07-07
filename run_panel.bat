@echo off
title Gold Genious
cd /d "%~dp0"
set PY=python
where python >nul 2>&1 || set PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
"%PY%" -m pip install -r requirements.txt --quiet
"%PY%" control_panel.py
