@echo off
REM Builds the stand-alone release: dist\LCRSweep\LCRSweep.exe and dist\LCRSweep-<version>-windows.zip
REM (no Python needed on the lab PC). See packaging\make_release.py for the steps.
cd /d "%~dp0"
call .venv\Scripts\activate.bat
pip install pyinstaller
python packaging\make_release.py
pause
