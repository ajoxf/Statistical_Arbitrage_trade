@echo off
:: ============================================================
:: StatArb Pro - One-Click Launcher
:: ============================================================
:: Just double-click this file to start the application!
:: ============================================================

setlocal enabledelayedexpansion

title StatArb Pro

echo.
echo  ============================================================
echo       StatArb Pro - Statistical Arbitrage Trading
echo  ============================================================
echo.

:: Check for .env file
if not exist ".env" (
    echo  [!] No .env file found.
    echo.
    echo      Please create a .env file with your MT5 settings.
    echo      You can copy .env.example and edit it:
    echo.
    echo      1. Copy .env.example to .env
    echo      2. Edit .env with your MT5 credentials
    echo.
    pause
    exit /b 1
)

:: Check Python installation
echo  [1/3] Checking Python installation...

py --version >nul 2>&1
if errorlevel 1 (
    python --version >nul 2>&1
    if errorlevel 1 (
        echo.
        echo  [ERROR] Python is not installed!
        echo.
        echo  Please install Python 3.9 or higher:
        echo  https://www.python.org/downloads/
        echo.
        echo  IMPORTANT: During installation, check the box that says
        echo  "Add Python to PATH"
        echo.
        pause
        exit /b 1
    )
    set "PY=python"
    set "PIP=python -m pip"
) else (
    set "PY=py"
    set "PIP=py -m pip"
)

for /f "tokens=2" %%i in ('%PY% --version 2^>^&1') do set PYVER=%%i
echo       Python %PYVER% found

:: Install dependencies (only if needed)
echo.
echo  [2/3] Checking dependencies...

:: Check if Flask is installed as a proxy for all dependencies
%PY% -c "import flask" >nul 2>&1
if errorlevel 1 (
    echo       Installing required packages (first time only)...
    echo       This may take a few minutes...
    echo.
    %PIP% install -r requirements.txt --quiet
    if errorlevel 1 (
        echo.
        echo  [ERROR] Failed to install dependencies.
        echo  Please check your internet connection and try again.
        echo.
        pause
        exit /b 1
    )

    :: Install MetaTrader5 separately (Windows only)
    echo       Installing MetaTrader5 connector...
    %PIP% install MetaTrader5 --quiet
)

echo       All dependencies ready

:: Launch the application
echo.
echo  [3/3] Starting StatArb Pro...
echo.
echo  ============================================================
echo   The application will open in your browser automatically.
echo   Keep this window open while using the application.
echo   Press 'q' to quit.
echo  ============================================================
echo.

%PY% launcher.py

pause
