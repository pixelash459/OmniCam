@echo off
rem build-exe.bat — build the standalone OmniCam.exe (onefile, windowed).
rem Requires: pc\.venv with requirements.txt installed, plus PyInstaller:
rem   .venv\Scripts\pip install pyinstaller
rem Output: pc\dist\OmniCam.exe
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\pyinstaller.exe (
    echo PyInstaller not found in .venv - installing...
    .venv\Scripts\python -m pip install pyinstaller
)
.venv\Scripts\pyinstaller --noconfirm --clean --onefile --windowed ^
    --name OmniCam ^
    --collect-binaries av ^
    --exclude-module tkinter ^
    --exclude-module PyQt5 ^
    OmniCamEntry.py
echo.
echo Done: %~dp0dist\OmniCam.exe
endlocal
