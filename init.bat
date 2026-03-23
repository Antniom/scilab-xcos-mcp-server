@echo off
setlocal enabledelayedexpansion

echo ################################################
echo # Scilab Xcos MCP Server Initialization        #
echo ################################################

:: 1. Auto-discover Scilab Root
set SCILAB_ROOT=
for /f "delims=" %%i in ('where scilab 2^>nul') do (
    set SCILAB_BIN_PATH=%%i
    :: Remove \bin\scilab.bat or similar to get root
    for %%j in ("!SCILAB_BIN_PATH!") do set SCILAB_ROOT=%%~dpj..
    call :VALIDATE_ROOT "!SCILAB_ROOT!"
    if not errorlevel 1 goto :FOUND_SCILAB
)

:: Check Progam Files
if exist "C:\Program Files\scilab-2026.0.1" (
    set SCILAB_ROOT=C:\Program Files\scilab-2026.0.1
    call :VALIDATE_ROOT "!SCILAB_ROOT!"
    if not errorlevel 1 goto :FOUND_SCILAB
)

:: Check common workspace locations
if exist "..\scilab-2026.0.1\scilab-2026.0.1\scilab" (
    set SCILAB_ROOT=..\scilab-2026.0.1\scilab-2026.0.1\scilab
    call :VALIDATE_ROOT "!SCILAB_ROOT!"
    if not errorlevel 1 goto :FOUND_SCILAB
)

echo [!] Scilab not found in PATH or common locations.
echo [!] Please ensure Scilab is installed and added to PATH, or manually edit init.bat.
pause
exit /b 1

:FOUND_SCILAB
for %%k in ("!SCILAB_ROOT!") do set SCILAB_ROOT=%%~fk
echo [OK] Found Scilab at: !SCILAB_ROOT!
:: Save Scilab path for other scripts
echo !SCILAB_ROOT!> .scilab_path

:: 2. Auto-discover Workspace Root
set WORKSPACE_ROOT=%CD%\..
echo [OK] Found Workspace at: !WORKSPACE_ROOT!

:: 3. Run Data Staging
echo [STAGING] Populating local data directory...
python setup_data.py --scilab "!SCILAB_ROOT!" --workspace "!WORKSPACE_ROOT!" --target data

if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Data staging failed.
    pause
    exit /b 1
)

echo [SUCCESS] MCP Server is initialized and ready.
pause
exit /b 0

:VALIDATE_ROOT
if exist "%~1\bin\WScilex-cli.exe" exit /b 0
if exist "%~1\bin\scilab-cli.exe" exit /b 0
if exist "%~1\bin\scilab-cli" exit /b 0
if exist "%~1\bin\scilab.bat" exit /b 0
exit /b 1
