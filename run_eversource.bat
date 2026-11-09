@echo off
setlocal
cd /d "%~dp0"

set "PY=scripts\.venv\Scripts\python.exe"
set "PYW=scripts\.venv\Scripts\pythonw.exe"

if not exist "%PY%" (
    echo.
    echo ERROR: Python environment not found.
    echo Run setup_eversource.bat first.
    echo.
    pause
    exit /b 1
)

if not exist "%PYW%" (
    echo.
    echo ERROR: pythonw.exe not found in scripts\.venv.
    echo Run setup_eversource.bat again.
    echo.
    pause
    exit /b 1
)

"%PY%" -c "import tkinter, psutil; import sys; sys.path.insert(0, r'scripts'); import tx_veg_bakery_gui" >nul 2>&1
if errorlevel 1 (
    echo.
    echo ERROR: GUI startup check failed.
    echo Run setup_eversource.bat again.
    echo.
    pause
    exit /b 1
)

start "" "%PYW%" "scripts\tx_veg_bakery_gui.py"
exit /b 0
