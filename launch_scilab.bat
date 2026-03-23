@echo off
setlocal

:: Enable Virtual Terminal Processing so ANSI escapes work in cmd
reg add HKCU\Console /v VirtualTerminalLevel /t REG_DWORD /d 1 /f >nul 2>&1

:: Black background, white text — kills the blinding white
color 00

title Scilab Xcos Daemon
mode con: cols=72 lines=32

:: Check Node.js
where node >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    color 07
    echo.
    echo  [!] Node.js not found.
    echo  [!] Download from: https://nodejs.org  ^(LTS version^)
    echo  [!] Then re-run this script.
    echo.
    pause
    exit /b 1
)

:: First-run: install dependencies
if not exist "%~dp0launcher\node_modules" (
    color 07
    echo  Installing launcher dependencies ^(first run only^)...
    cd /d "%~dp0launcher"
    npm install --silent
    if %ERRORLEVEL% NEQ 0 (
        echo  [!] npm install failed.
        pause
        exit /b 1
    )
)

:: Run from project root so .scilab_path resolves correctly
cd /d "%~dp0"
node "%~dp0launcher\launcher.js"

if %ERRORLEVEL% NEQ 0 pause
