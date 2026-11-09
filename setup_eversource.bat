@echo off
setlocal
cd /d "%~dp0"

echo ================================================================
echo NM GROUP - TX VEG DELIVERY BAKERY - SETUP
echo ================================================================
echo.

where py >nul 2>&1
if errorlevel 1 (
    echo ERROR: Windows Python Launcher ^(py.exe^) was not found.
    echo Install 64-bit Python 3.14, then run this BAT again.
    echo.
    pause
    exit /b 1
)

for %%V in (3.14 3.13 3.12 3.11 3.10) do (
    py -%%V -c "import sys; print(sys.version)" >nul 2>&1
    if not errorlevel 1 (
        echo Using Python %%V
        py -%%V "scripts\setup_eversource_tester.py"
        goto :done
    )
)

echo ERROR: No supported 64-bit Python 3.10-3.14 installation was found.
echo Install Python 3.14 and run this BAT again.
pause
exit /b 1

:done
if errorlevel 1 (
    echo.
    echo SETUP FAILED.
    pause
    exit /b 1
)

echo.
echo Setup complete.
pause
