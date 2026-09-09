<#
  Codex 코드 리뷰 하네스 (Windows).

  scripts/codex_review.sh 의 PowerShell 판이다. 이 저장소의 운영 환경은
  Windows 인데(§11.2) 리뷰 하네스만 bash 전용이라 정작 노트북에서 돌릴 수
  없었다. Git Bash 를 깔라고 하는 대신 여기에 맞춘다.

  사용:
    powershell -ExecutionPolicy Bypass -File windows\codex_review.ps1
    powershell -ExecutionPolicy Bypass -File windows\codex_review.ps1 -Mode staged
    powershell -ExecutionPolicy Bypass -File windows\codex_review.ps1 -Mode since -Since HEAD~5
    powershell -ExecutionPolicy Bypass -File windows\codex_review.ps1 -Model gpt-5.6-sol

  결과는 docs\review\codex-<타임스탬프>.md 에 저장되고 화면에도 나온다.

  사전 조건
    npm install -g @openai/codex
    codex login
  회사 계정으로 로그인된 브라우저는 피할 것 (§11.8 4번).
#>

param(
    [ValidateSet("full", "worktree", "staged", "since")]
    [string]$Mode = "full",
    [string]$Since = "HEAD~1",
    # 모델을 고정한다. codex 의 기본값은 계정 종류에 따라 거부될 수 있다 —
    # ChatGPT 계정 로그인에서 gpt-5.4 기본값이 400 으로 튕겼다(실제로 겪음).
    # CLI 자체 안내: "GPT-5.4 is no longer available. Codex now uses GPT-5.6 Terra".
    [string]$Model = "gpt-5.6-terra"
)

$ErrorActionPreference = "Stop"

# 한글이 깨지지 않게 콘솔·파이프를 UTF-8 로 고정한다.
# 이걸 빼면 리뷰 프롬프트와 보고서가 둘 다 깨진다 (cp949 기본).
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
    Write-Host @"
codex CLI 를 찾을 수 없습니다.

  npm install -g @openai/codex
  codex login

리뷰어를 Codex 로 고정하는 것이 이 저장소의 규칙입니다(CLAUDE.md).
Codex 를 쓸 수 없는 환경이라면 그 사실을 리포트에 명시하고,
다른 모델의 리뷰를 Codex 리뷰라고 부르지 마십시오.
"@ -ForegroundColor Red
    exit 127
}

$PromptFile = ".codex\prompts\review.md"
if (-not (Test-Path $PromptFile)) {
    Write-Host "리뷰 체크리스트가 없습니다: $PromptFile" -ForegroundColor Red
    exit 1
}

switch ($Mode) {
    "worktree" { $Diff = (git diff | Out-String);            $Scope = "워킹 트리 변경분" }
    "staged"   { $Diff = (git diff --cached | Out-String);   $Scope = "스테이지된 변경분" }
    "since"    { $Diff = (git diff $Since | Out-String);     $Scope = "$Since 이후 변경분" }
    "full"     { $Diff = "";                                 $Scope = "저장소 전체" }
}

if ($Mode -ne "full" -and [string]::IsNullOrWhiteSpace($Diff)) {
    Write-Host "리뷰할 변경분이 없습니다 ($Scope)."
    exit 0
}

$lines = New-Object System.Collections.Generic.List[string]
$lines.Add((Get-Content $PromptFile -Raw -Encoding UTF8))
$lines.Add("")
$lines.Add("---")
$lines.Add("")
$lines.Add("## 리뷰 범위: $Scope")
$lines.Add("")
if ($Mode -eq "full") {
    $lines.Add("저장소 전체를 리뷰한다. 진입점은 app/cli.py 이고,")
    $lines.Add("가장 위험한 코드는 app/data/pit_store.py, app/backtest/engine.py,")
    $lines.Add("app/evaluator/, app/pipeline/daily.py, app/pipeline/positions.py,")
    $lines.Add("app/backtest/constraints.py, app/execution/divergence.py 다.")
    $lines.Add("필요한 파일을 직접 열어 읽어라.")
} else {
    $lines.Add('```diff')
    $lines.Add($Diff.TrimEnd())
    $lines.Add('```')
}

# BOM 없는 UTF-8 로 쓴다. BOM 이 붙으면 프롬프트 첫 글자가 깨져서 들어간다.
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("codex-review-" + [guid]::NewGuid().ToString("N") + ".md")
[System.IO.File]::WriteAllText($tmp, ($lines -join "`n"), (New-Object System.Text.UTF8Encoding($false)))

$outDir = "docs\review"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$out = Join-Path $outDir ("codex-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".md")

Write-Host "Codex 리뷰 시작 ($Scope) · 모델 $Model"
Write-Host "몇 분 걸립니다. 중간에 끊지 마십시오."
Write-Host ""

try {
    # 파일 리다이렉션은 cmd 로 넘긴다 — PowerShell 5.1 에는 '<' 입력 리다이렉션이 없고,
    # Get-Content 파이프는 인코딩을 한 번 더 건드린다.
    $modelArg = if ([string]::IsNullOrWhiteSpace($Model)) { "" } else { "-m $Model " }
    & cmd /c "codex exec $modelArg--skip-git-repo-check - < `"$tmp`" 2>&1" | Tee-Object -FilePath $out
} finally {
    Remove-Item $tmp -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "리뷰 결과: $out"
Write-Host "이 파일은 커밋하십시오 — 실패 로그가 자산입니다 (§18)."
