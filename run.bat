@echo off
REM ===========================================================================
REM  tuimian launcher  --  demo entry point
REM
REM  IMPORTANT: this file is intentionally ASCII-ONLY.
REM  cmd.exe parses .bat with the console code page (GBK on zh-CN Windows),
REM  so Chinese text here gets mangled into stray "commands" and the script
REM  dies. All Chinese messages are printed by run.py instead (it controls its
REM  own encoding). Do not add Chinese to this file.
REM
REM  Usage:  run.bat          -> port 8000
REM          run.bat 8080     -> other port
REM          set RELOAD=1 & run.bat   -> dev mode (auto restart)
REM ===========================================================================
setlocal
cd /d "%~dp0"

REM  Python lookup order: %PYTHON% env var -> .venv next to repo -> py launcher -> python
set "PY=%PYTHON%"
set "PYARGS="
if not "%PY%"=="" goto :havepy
if exist "%~dp0.venv\Scripts\python.exe" (
  set "PY=%~dp0.venv\Scripts\python.exe"
  goto :havepy
)
where py >nul 2>nul
if not errorlevel 1 (
  set "PY=py"
  set "PYARGS=-3"
  goto :havepy
)
set "PY=python"
:havepy

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8000"

set "RELOADARG="
if "%RELOAD%"=="1" set "RELOADARG=--reload"

"%PY%" %PYARGS% "%~dp0run.py" --port %PORT% %RELOADARG%
set "RC=%ERRORLEVEL%"

echo.
echo   [run.bat] server exited with code %RC%
pause
endlocal
exit /b %RC%
