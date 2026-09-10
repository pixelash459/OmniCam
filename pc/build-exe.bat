@echo off
rem Build a standalone OmniCam PC tree + Inno Setup installer.
rem Output:
rem   pc\dist\OmniCam\OmniCam.exe     (portable onedir)
rem   dist\OmniCam-PC-1.1.9-Setup.exe (this repo's dist\, double-click installer)
setlocal EnableExtensions
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Missing pc\.venv — create it first:  python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

echo ==^> icon
".venv\Scripts\python.exe" packaging\make_icon.py || exit /b 1

if not exist ".venv\Scripts\pyinstaller.exe" (
    echo ==^> installing PyInstaller
    ".venv\Scripts\python.exe" -m pip install -q pyinstaller || exit /b 1
)

echo ==^> PyInstaller onedir
if exist dist\OmniCam rd /s /q dist\OmniCam
".venv\Scripts\pyinstaller.exe" --noconfirm --clean OmniCam.spec || exit /b 1

if not exist "dist\OmniCam\OmniCam.exe" (
    echo PyInstaller did not produce dist\OmniCam\OmniCam.exe
    exit /b 1
)

set "ISCC="
if exist "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if "%ISCC%"=="" (
    echo ==^> Inno Setup not found — installing (per-user)
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0packaging\install-inno.ps1" || exit /b 1
    if exist "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
    if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
)
if "%ISCC%"=="" (
    echo ISCC.exe still not found after Inno Setup install.
    exit /b 1
)

echo ==^> Inno Setup  "%ISCC%"
"%ISCC%" packaging\OmniCam.iss || exit /b 1

echo.
echo Done:
echo   %~dp0dist\OmniCam\OmniCam.exe
echo   %~dp0..\dist\OmniCam-PC-1.1.9-Setup.exe
endlocal
