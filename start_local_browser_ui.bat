@echo off
setlocal

cd /d "%~dp0"

set "PYTHON_EXE=%~dp0..\.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    set "PYTHON_EXE=python"
)

set "XCOS_SERVER_MODE=http"
set "XCOS_VALIDATION_MODE=poll"
set "PORT=7860"

echo Starting Scilab Xcos local browser UI...
echo Open http://127.0.0.1:7860/workflow-ui after the server starts.
echo.
echo If you want Scilab verification to work in this mode, also run launch_scilab.bat in another window.
echo.

"%PYTHON_EXE%" server.py

if errorlevel 1 pause
