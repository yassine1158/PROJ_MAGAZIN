@echo off
chcp 65001 >nul
cd /d "%~dp0.."
if exist venv\Scripts\activate.bat call venv\Scripts\activate.bat
python -m pip install -q cryptography >nul 2>&1
python outils\licences.py
echo.
pause
