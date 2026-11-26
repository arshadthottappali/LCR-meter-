@echo off
REM Setup: creates a Python virtual environment (.venv) and installs the libraries.
REM Uses the python.org Python via the "py" launcher when available (Anacondas Python ships old
REM DLLs that break Qt/PySide6). Safe to run again: .venv is rebuilt if the Python version changed.
cd /d "%~dp0"
set PY=
py -3 --version >nul 2>&1 && set PY=py -3
if not defined PY (python --version >nul 2>&1 && set PY=python)
if not defined PY (echo Python 3.10+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
%PY% -c "import sys,os;sys.exit(1 if os.path.isdir(os.path.join(sys.base_prefix,chr(99)+chr(111)+chr(110)+chr(100)+chr(97)+chr(45)+chr(109)+chr(101)+chr(116)+chr(97))) else 0)"
if errorlevel 1 (
  echo.
  echo WARNING: the Python found is an Anaconda/conda Python. Qt/PySide6 often fails to load with it
  echo ^("DLL load failed"^). Install Python from https://www.python.org/downloads/ ^(tick "py launcher"^)
  echo and run this setup again.
  echo.
)
for /f "delims=" %%v in ('%PY% -c "import sys;print(str(sys.version_info[0])+chr(46)+str(sys.version_info[1]))"') do set PYVER=%%v
echo Using Python %PYVER% (%PY%)
set VENVVER=
if exist .venv\Scripts\python.exe (
  for /f "delims=" %%v in ('.venv\Scripts\python.exe -c "import sys;print(str(sys.version_info[0])+chr(46)+str(sys.version_info[1]))"') do set VENVVER=%%v
)
if exist .venv if not "%VENVVER%"=="%PYVER%" (
  echo The existing .venv was made with Python %VENVVER%, rebuilding it with Python %PYVER% ...
  rmdir /s /q .venv
)
if not exist .venv\Scripts\python.exe %PY% -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install --upgrade -r requirements.txt || (echo. & echo Installing the libraries FAILED - see the messages above. & pause & exit /b 1)
python -c "import numpy, scipy, matplotlib, pyqtgraph; import PySide6.QtCore" || (echo. & echo A library does not load - see the message above. Send it to the developer. & pause & exit /b 1)
echo.
echo Setup finished. Start the program with run_lcr.bat
pause
