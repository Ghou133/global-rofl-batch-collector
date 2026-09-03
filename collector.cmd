@echo off
setlocal
set "PROJECT_DIR=%~dp0"
if exist "%PROJECT_DIR%.venv\Scripts\python.exe" (
  "%PROJECT_DIR%.venv\Scripts\python.exe" -m kr_rofl_collector %*
) else (
  py -3.11 -m kr_rofl_collector %*
)
exit /b %ERRORLEVEL%

