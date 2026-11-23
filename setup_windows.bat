@echo off
REM One-time setup: creates a Python virtual environment and installs the libraries.
cd /d "%~dp0"
python -m venv .venv || (echo Python 3.10+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
echo.
echo Setup finished. Start the program with run_lcr.bat
pause
