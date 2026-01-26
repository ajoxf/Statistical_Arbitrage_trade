@echo off
:: ============================================================
:: StatArb Pro - Windows Installer Build Script
:: ============================================================
::
:: This script builds the complete Windows installer:
::   1. Creates Python executable using PyInstaller
::   2. Packages into installer using Inno Setup
::
:: Prerequisites:
::   - Python 3.9+ with pip
::   - Inno Setup 6.x (https://jrsoftware.org/isinfo.php)
::
:: Usage:
::   build.bat
::
:: ============================================================

setlocal enabledelayedexpansion

:: Colors for output (Windows 10+)
set "GREEN=[92m"
set "RED=[91m"
set "YELLOW=[93m"
set "CYAN=[96m"
set "RESET=[0m"

echo.
echo %CYAN%============================================================%RESET%
echo %CYAN%         StatArb Pro - Installer Build Script              %RESET%
echo %CYAN%============================================================%RESET%
echo.

:: Check if running from correct directory
if not exist "statarb.spec" (
    echo %RED%ERROR: Please run this script from the installer directory%RESET%
    echo        cd installer
    echo        build.bat
    exit /b 1
)

:: Set paths
set "PROJECT_ROOT=%~dp0.."
set "INSTALLER_DIR=%~dp0"
set "DIST_DIR=%INSTALLER_DIR%dist"
set "OUTPUT_DIR=%INSTALLER_DIR%output"

:: ============================================================
:: Step 1: Check Prerequisites
:: ============================================================
echo %YELLOW%[1/5] Checking prerequisites...%RESET%

:: Check Python (try py first, then python)
py --version >nul 2>&1
if errorlevel 1 (
    python --version >nul 2>&1
    if errorlevel 1 (
        echo %RED%ERROR: Python not found. Please install Python 3.9+%RESET%
        echo        https://www.python.org/downloads/
        exit /b 1
    )
    set "PY=python"
    set "PIP=pip"
) else (
    set "PY=py"
    set "PIP=py -m pip"
)
echo       Python: OK

:: Check pip
%PIP% --version >nul 2>&1
if errorlevel 1 (
    echo %RED%ERROR: pip not found%RESET%
    exit /b 1
)
echo       pip: OK

:: Check Inno Setup
set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" (
    set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
)
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" (
    set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
)

if "%ISCC%"=="" (
    echo %YELLOW%WARNING: Inno Setup not found. Installer will not be created.%RESET%
    echo          Download from: https://jrsoftware.org/isinfo.php
    set "SKIP_INNO=1"
) else (
    echo       Inno Setup: OK
)

echo.

:: ============================================================
:: Step 2: Install Dependencies
:: ============================================================
echo %YELLOW%[2/5] Installing dependencies...%RESET%

%PIP% install pyinstaller --quiet
if errorlevel 1 (
    echo %RED%ERROR: Failed to install PyInstaller%RESET%
    exit /b 1
)

%PIP% install -r "%PROJECT_ROOT%\requirements.txt" --quiet
if errorlevel 1 (
    echo %RED%ERROR: Failed to install project dependencies%RESET%
    exit /b 1
)

echo       Dependencies installed
echo.

:: ============================================================
:: Step 3: Create placeholder assets if missing
:: ============================================================
echo %YELLOW%[3/5] Checking assets...%RESET%

if not exist "%INSTALLER_DIR%assets\icon.ico" (
    echo       Creating placeholder icon...
    %PY% -c "
import struct
# Create a minimal valid ICO file (16x16, 1 color)
ico_header = struct.pack('<HHH', 0, 1, 1)  # Reserved, Type=ICO, Count=1
ico_entry = struct.pack('<BBBBHHII', 16, 16, 0, 0, 1, 32, 40+16*16*4, 22)
bmp_header = struct.pack('<IiiHHIIiiII', 40, 16, 32, 1, 32, 0, 16*16*4, 0, 0, 0, 0)
# Red/pink colored pixels
pixels = b'\x60\x45\xe9\xff' * 16 * 16
with open('assets/icon.ico', 'wb') as f:
    f.write(ico_header + ico_entry + bmp_header + pixels)
print('       Icon created')
"
)

if not exist "%INSTALLER_DIR%assets\wizard_large.bmp" (
    echo       Creating wizard images...
    %PY% -c "
import struct
# Create 164x314 BMP for wizard large image
w, h = 164, 314
row_size = (w * 3 + 3) // 4 * 4
img_size = row_size * h
file_size = 54 + img_size

header = struct.pack('<2sIHHI', b'BM', file_size, 0, 0, 54)
info = struct.pack('<IiiHHIIiiII', 40, w, h, 1, 24, 0, img_size, 0, 0, 0, 0)

# Dark blue gradient background
rows = []
for y in range(h):
    row = b''
    for x in range(w):
        # RGB (BGR in BMP)
        b = int(30 + (y / h) * 20)
        g = int(26 + (y / h) * 15)
        r = int(46 + (y / h) * 25)
        row += bytes([b, g, r])
    row += b'\x00' * (row_size - w * 3)
    rows.append(row)

with open('assets/wizard_large.bmp', 'wb') as f:
    f.write(header + info + b''.join(rows))
print('       Wizard large image created')
"
    %PY% -c "
import struct
# Create 55x55 BMP for wizard small image
w, h = 55, 55
row_size = (w * 3 + 3) // 4 * 4
img_size = row_size * h
file_size = 54 + img_size

header = struct.pack('<2sIHHI', b'BM', file_size, 0, 0, 54)
info = struct.pack('<IiiHHIIiiII', 40, w, h, 1, 24, 0, img_size, 0, 0, 0, 0)

# Accent color circle on dark background
rows = []
cx, cy = w // 2, h // 2
for y in range(h):
    row = b''
    for x in range(w):
        dist = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
        if dist < 20:
            row += bytes([96, 69, 233])  # Accent color (BGR)
        else:
            row += bytes([46, 33, 26])  # Dark background
    row += b'\x00' * (row_size - w * 3)
    rows.append(row)

with open('assets/wizard_small.bmp', 'wb') as f:
    f.write(header + info + b''.join(rows))
print('       Wizard small image created')
"
)

echo       Assets ready
echo.

:: ============================================================
:: Step 4: Build with PyInstaller
:: ============================================================
echo %YELLOW%[4/5] Building executable with PyInstaller...%RESET%
echo       This may take several minutes...
echo.

:: Clean previous build
if exist "%DIST_DIR%" rmdir /s /q "%DIST_DIR%"
if exist "%INSTALLER_DIR%build" rmdir /s /q "%INSTALLER_DIR%build"

:: Run PyInstaller
cd /d "%INSTALLER_DIR%"
pyinstaller statarb.spec --noconfirm --clean

if errorlevel 1 (
    echo %RED%ERROR: PyInstaller build failed%RESET%
    exit /b 1
)

echo.
echo       %GREEN%Executable built successfully%RESET%
echo.

:: ============================================================
:: Step 5: Create Installer with Inno Setup
:: ============================================================
if defined SKIP_INNO (
    echo %YELLOW%[5/5] Skipping installer creation (Inno Setup not found)%RESET%
    echo.
    echo %GREEN%Build complete!%RESET%
    echo Executable location: %DIST_DIR%\StatArbPro\StatArbPro.exe
    goto :end
)

echo %YELLOW%[5/5] Creating installer with Inno Setup...%RESET%

:: Create output directory
if not exist "%OUTPUT_DIR%" mkdir "%OUTPUT_DIR%"

:: Create placeholder Quick Start Guide
if not exist "%INSTALLER_DIR%assets\Quick_Start_Guide.pdf" (
    echo Quick Start Guide - See online documentation > "%INSTALLER_DIR%assets\Quick_Start_Guide.pdf"
)

:: Run Inno Setup Compiler
"%ISCC%" "%INSTALLER_DIR%setup.iss"

if errorlevel 1 (
    echo %RED%ERROR: Inno Setup build failed%RESET%
    exit /b 1
)

echo.
echo %GREEN%============================================================%RESET%
echo %GREEN%                    BUILD SUCCESSFUL!                       %RESET%
echo %GREEN%============================================================%RESET%
echo.
echo   Executable: %DIST_DIR%\StatArbPro\StatArbPro.exe
echo   Installer:  %OUTPUT_DIR%\StatArbPro_Setup_1.0.0.exe
echo.

:end
pause
