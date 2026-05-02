@echo off
REM Build agent.exe with PyInstaller.
REM Run from the windows-agent\ directory on a Windows host with Python 3.11+.

setlocal
where pyinstaller >nul 2>nul
if errorlevel 1 (
    echo Installing build dependencies...
    pip install -r requirements.txt || goto :error
)

echo Building agent.exe...
pyinstaller --onefile --name agent ^
    --add-data "config.json;." ^
    agent.py || goto :error

echo.
echo Done. Output: dist\agent.exe
echo Bundle dist\agent.exe + config.json next to it for distribution.
exit /b 0

:error
echo Build failed.
exit /b 1
