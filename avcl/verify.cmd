@echo off
setlocal
cd /d "%~dp0"
set "AVCL_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if "%~1"=="--postgres" if exist ".venv\Scripts\python.exe" set "AVCL_PYTHON=%CD%\.venv\Scripts\python.exe"
if exist "%AVCL_PYTHON%" goto bundled
python -X utf8 scripts\verify.py %*
exit /b %errorlevel%
:bundled
"%AVCL_PYTHON%" -X utf8 scripts\verify.py %*
exit /b %errorlevel%
