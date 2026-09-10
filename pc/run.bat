@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m omnicam %*
) else (
    python -m omnicam %*
)
