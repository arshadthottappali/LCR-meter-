@echo off
REM Setup: creates a Python virtual environment (.venv) and installs the libraries.
REM Safe to run again: if .venv was made with a different Python version it is rebuilt.
cd /d "%~dp0"
python --version >nul 2>&1 || (echo Python 3.10+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
for /f "delims=" %%v in ('python -c "import sys;print(str(sys.version_info[0])+chr(46)+str(sys.version_info[1]))"') do set PYVER=%%v
if exist .venv\Scripts\python.exe (
  for /f "delims=" %%v in ('.venv\Scripts\python.exe -c "import sys;print(str(sys.version_info[0])+chr(46)+str(sys.version_info[1]))"') do set VENVVER=%%v
)
if exist .venv if not "%VENVVER%"=="%PYVER%" (
  echo The existing .venv was made with Python %VENVVER%, but "python" is now %PYVER%. Rebuilding .venv ...
  rmdir /s /q .venv
)
if not exist .venv\Scripts\python.exe python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install --upgrade -r requirements.txt || (echo. & echo Installing the libraries FAILED - see the messages above. & pause & exit /b 1)
python -c "import numpy, scipy, PySide6, pyqtgraph, matplotlib" || (echo. & echo A library does not load. Delete the .venv folder and run this setup again. & pause & exit /b 1)
echo.
echo Setup finished. Start the program with run_lcr.bat
pause
