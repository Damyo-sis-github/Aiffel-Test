@echo off
REM §11.2 evolve 실행 래퍼.
REM   --if-pending : state\trigger_pending.json 이 없으면 즉시 종료 → 크레딧 소모 0
REM   낮 시간(평일 08:00-18:00 KST) 호출은 거부하고 다음 창으로 이월한다 (#29).
REM 이 작업만 "로그온 시에만 실행" 이다 — Claude Code 인증 토큰이 로그온 세션에 있기 때문.

setlocal
set PYTHONUTF8=1
set QUANT_ROOT=%~dp0..
cd /d "%QUANT_ROOT%"

call .venv\Scripts\python.exe -m app.cli evolve --if-pending
set RC=%ERRORLEVEL%

REM 실패해도 당일 재시도하지 않는다. 다음날 19:00 (§11.3).
if %RC% NEQ 0 echo evolve 종료 코드 %RC% — 당일 재시도 없음.

endlocal
exit /b 0
