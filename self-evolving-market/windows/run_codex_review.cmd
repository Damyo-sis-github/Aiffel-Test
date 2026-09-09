@echo off
REM Codex 리뷰 — cmd 에서 바로 실행하는 래퍼.
REM
REM   windows\run_codex_review.cmd            전체 리뷰
REM   windows\run_codex_review.cmd staged     스테이지된 변경분만
REM
REM PowerShell 실행 정책 때문에 .ps1 을 직접 못 도는 경우를 위해 둔다.

setlocal
set MODE=%1
if "%MODE%"=="" set MODE=full

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0codex_review.ps1" -Mode %MODE%
endlocal
exit /b %ERRORLEVEL%
