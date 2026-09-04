@echo off
REM weekly / monthly / healthcheck 공용 래퍼.

setlocal
set PYTHONUTF8=1
set QUANT_ROOT=%~dp0..
cd /d "%QUANT_ROOT%"

call .venv\Scripts\python.exe -m app.cli %*
endlocal
exit /b %ERRORLEVEL%
