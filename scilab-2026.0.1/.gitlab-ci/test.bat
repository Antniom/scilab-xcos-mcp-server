REM Execute module test for a module named %TEST%, download and install latest build if needed

@echo on
set INSTALL_DIR=%SCILAB_COMMON_PATH%\%SCI_VERSION_STRING%\install
set INSTALL_LOCK=%SCILAB_COMMON_PATH%\%SCI_VERSION_STRING%\install.lock
set INSTALL_FAIL=%SCILAB_COMMON_PATH%\%SCI_VERSION_STRING%\install.failed
set SCIHOME=%SCILAB_COMMON_PATH%\%SCI_VERSION_STRING%\scihome\%SCI_VERSION_STRING%-%TEST%-%CI_CONCURRENT_ID%
set LOG_PATH=%SCI_VERSION_STRING%\%ARCH%-windows

REM Create log folder
if not exist %LOG_PATH% mkdir %LOG_PATH%

REM check if installation is running
:wait_install
if exist "%INSTALL_LOCK%" (
  echo "Wait installation"
  sleep 30
  goto :wait_install
)

if exist "%INSTALL_FAIL%" (
  echo "Skip test - Installation failed - check the test job of "
  rem display the module name that fails the install
  cat %INSTALL_FAIL%
  rem return job success to ease finding the one that failed the installation
  exit 1
)

REM already Installed
if exist "%INSTALL_DIR%" (
  echo "Installation step skipped: Scilab version already installed."
  goto :installed
)

REM create lock file
type nul > "%INSTALL_LOCK%"
if errorlevel 1 (
  goto :wait_install
)

call "%SCI_VERSION_STRING%.bin.%ARCH%.exe" ^
  /TASKS=!desktopicon,!AssociateSCESCI,!AssociateTSTDEM,!AssociateSCICOS,!AssociateSOD ^
  /NOICONS /SUPPRESSMSGBOXES /SILENT /SP- ^
  /LOG="%LOG_PATH%\test_iss_install_%CI_COMMIT_SHORT_SHA%.log" ^
  /DIR="%INSTALL_DIR%"

if errorlevel 1 (
  echo "Scilab Installation failed"
  echo "%TEST%" > "%INSTALL_FAIL%"
  del "%INSTALL_LOCK%"
  exit 1
)

del "%INSTALL_LOCK%"

:installed


@echo on
setlocal EnableExtensions

rem can append in case of restarting a test job with same conccurency
if exist -d "%SCIHOME%" rmdir "%SCIHOME%" 
mkdir "%SCIHOME%"

REM check if Scilex exists
if not exist "%INSTALL_DIR%\bin\Scilex.exe" (
  echo "%INSTALL_DIR%\bin\Scilex.exe does not exist."
  exit 1
)

@echo on
call "%INSTALL_DIR%\bin\Scilex.exe" -scihome "%SCIHOME%" -quit -e "test_run('%TEST%',[],[],'%LOG_PATH%\%TEST%.xml'); [__msg__, __err__] = lasterror(), exit(__err__)"
if errorlevel 1 (
  echo "Scilab exit with code %errorlevel%"
  exit 1
)

rem fail without xml report
copy "%LOG_PATH%\%TEST%.xml" "%SCILAB_COMMON_PATH%\%SCI_VERSION_STRING%\test\"
if errorlevel 1 exit 1

rem copy the logs
if exist "%LOG_PATH%\test_iss_install_%CI_COMMIT_SHORT_SHA%.log" copy "%LOG_PATH%\test_iss_install_%CI_COMMIT_SHORT_SHA%.log" "%SCILAB_COMMON_PATH%\%SCI_VERSION_STRING%\log\"
if errorlevel 1 exit 1
