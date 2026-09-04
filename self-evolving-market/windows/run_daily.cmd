@echo off
REM §11.2 daily 실행 래퍼. 순수 Python — LLM 호출 없음.
REM 실패 시 3회 재시도(10분 간격) 후 텔레그램 알림 (§11.4).

setlocal
set PYTHONUTF8=1
set QUANT_ROOT=%~dp0..
cd /d "%QUANT_ROOT%"

set ATTEMPT=0
:retry
set /a ATTEMPT+=1
call .venv\Scripts\python.exe -m app.cli daily %* --push
if %ERRORLEVEL% EQU 0 goto done

if %ATTEMPT% GEQ 3 (
  echo daily 3회 실패. 종료 코드 %ERRORLEVEL%
  exit /b %ERRORLEVEL%
)
echo daily 실패 ^(시도 %ATTEMPT%/3^). 10분 후 재시도합니다.
timeout /t 600 /nobreak > nul
goto retry

:done
endlocal
exit /b 0
