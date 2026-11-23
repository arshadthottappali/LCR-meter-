@echo off
REM Builds a stand-alone LCRSweep.exe in dist\LCRSweep\ (no Python needed on the lab PC).
cd /d "%~dp0"
call .venv\Scripts\activate.bat
pip install pyinstaller
pyinstaller --noconfirm --windowed --name LCRSweep ^
  --add-data "lcr_app\instrument\commands_3532_50.json;lcr_app\instrument" run_lcr.py
echo Done: dist\LCRSweep\LCRSweep.exe
pause
