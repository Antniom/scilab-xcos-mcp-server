@echo off
setlocal

cd /d "%~dp0"

set "PYTHON_EXE=%~dp0..\.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    set "PYTHON_EXE=python"
)

if not exist ".scilab_path" (
    echo Scilab path has not been initialized yet.
    echo Run init.bat first.
    pause
    exit /b 1
)

set /p SCILAB_ROOT=<.scilab_path

if not exist "%SCILAB_ROOT%\bin\scilab-cli.exe" (
    echo Could not find scilab-cli.exe at:
    echo %SCILAB_ROOT%\bin\scilab-cli.exe
    echo.
    echo This mode needs a local Scilab CLI install.
    pause
    exit /b 1
)

set "SCILAB_BIN=%SCILAB_ROOT%\bin\scilab-cli.exe"
set "XCOS_SERVER_MODE=http"
set "XCOS_VALIDATION_MODE=subprocess"
set "PORT=7860"

echo Starting Hugging Face style local test mode...
echo Open http://127.0.0.1:7860/workflow-ui after the server starts.
echo MCP endpoint: http://127.0.0.1:7860/mcp
echo.

"%PYTHON_EXE%" server.py

if errorlevel 1 pause
