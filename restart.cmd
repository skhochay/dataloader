@echo off
rem ===================================================================
rem  DataLoader: stop everything, rebuild, start again.
rem
rem  Usage, from the folder holding manage.py:
rem
rem      restart.cmd              stop, build the frontend, migrate, start
rem      restart.cmd full         the same, plus pip install and npm install
rem      restart.cmd stop         stop only
rem
rem  The engine is stopped the way the app itself does it: by writing
rem  <data dir>\engine.stop and waiting. It finishes the file it is
rem  loading and exits, so a load in flight is never cut in half.
rem  Only if it ignores that for 20 seconds is it killed outright.
rem ===================================================================

setlocal EnableExtensions
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
if not defined DATALOADER_DATA_DIR set "DATALOADER_DATA_DIR=C:\DataLoader"
set "DATA=%DATALOADER_DATA_DIR%"
set "MODE=%~1"

echo.
echo === DataLoader restart ===
echo     project : %CD%
echo     data    : %DATA%
echo.

rem ---------------------------------------------------------------- checks
if not exist "manage.py" (
  echo [X] manage.py is not in this folder.
  echo     Put restart.cmd in the project root, next to manage.py.
  goto :fail
)
if not exist "%PY%" (
  echo [X] No virtualenv at %PY%
  echo     Create it first:
  echo         python -m venv .venv
  echo         .venv\Scripts\python.exe -m pip install -r requirements.txt
  goto :fail
)

rem ============================================================ 1. STOP
echo [1/4] Stopping...

rem Ask the engine to stop the polite way, then wait for its heartbeat to go.
if exist "%DATA%" (
  echo.>"%DATA%\engine.stop" 2>nul
)
set /a WAIT=0
:waitengine
if not exist "%DATA%\engine.heartbeat" goto engine_down
set /a WAIT+=1
if %WAIT% GEQ 20 goto engine_stuck
timeout /t 1 /nobreak >nul 2>&1
goto waitengine

:engine_stuck
echo     engine did not stop in 20s - forcing it.
goto kill_rest

:engine_down
echo     engine stopped.

:kill_rest
rem Close the windows this script opened last time.
taskkill /F /FI "WINDOWTITLE eq DataLoader web*"    >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq DataLoader engine*" >nul 2>&1

rem Catch anything started by hand outside this script. Matches only
rem Python processes whose command line runs one of our commands, so
rem other Python work on this machine is left alone.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*manage.py runserver*' -or $_.CommandLine -like '*manage.py run_engine*' -or $_.CommandLine -like '*manage.py run_worker*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1

rem A stale stop file would stop the next engine the moment it starts.
del /q "%DATA%\engine.stop" >nul 2>&1
echo     all stopped.

if /i "%MODE%"=="stop" (
  echo.
  echo Stopped. Nothing restarted ^(you passed "stop"^).
  goto :done
)

rem ========================================================= 2. DEPENDENCIES
if /i "%MODE%"=="full" (
  echo.
  echo [2/4] Installing dependencies ^(full^)...
  "%PY%" -m pip install -r requirements.txt
  if errorlevel 1 goto :fail_step
  pushd frontend
  call npm install
  if errorlevel 1 ( popd & goto :fail_step )
  popd
) else (
  echo.
  echo [2/4] Dependencies...
  if not exist "frontend\node_modules" (
    echo     node_modules missing - running npm install.
    pushd frontend
    call npm install
    if errorlevel 1 ( popd & goto :fail_step )
    popd
  ) else (
    echo     skipped ^(run "restart.cmd full" to reinstall^).
  )
)

rem ========================================================== 3. REBUILD
echo.
echo [3/4] Building the frontend...
pushd frontend
call npm run build
if errorlevel 1 ( popd & goto :fail_step )
popd
if not exist "loader\static\app\app.js" (
  echo [X] The build did not produce loader\static\app\app.js
  echo     Without it the dashboard loads but renders a blank page.
  goto :fail
)
echo     bundle written to loader\static\app\

echo.
echo     Applying migrations...
"%PY%" manage.py migrate
if errorlevel 1 goto :fail_step

rem ============================================================ 4. START
echo.
echo [4/4] Starting...
start "DataLoader web"    cmd /k %PY% manage.py runserver 8080
start "DataLoader engine" cmd /k %PY% manage.py run_engine

rem Give the server a moment, then check it actually answers.
timeout /t 5 /nobreak >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-WebRequest -Uri 'http://localhost:8080/' -UseBasicParsing -TimeoutSec 10; Write-Host ('    dashboard responded: HTTP ' + $r.StatusCode) } catch { Write-Host '    dashboard did not answer yet - check the DataLoader web window.' }"

echo.
echo === Done. Dashboard: http://localhost:8080 ===
echo     Two windows opened: "DataLoader web" and "DataLoader engine".
echo     Closing them, or running restart.cmd again, stops everything.
goto :done

:fail_step
echo.
echo [X] A step failed - see the output above. Nothing was restarted.

:fail
echo.
endlocal
exit /b 1

:done
echo.
endlocal
exit /b 0
