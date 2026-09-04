<#
  §11.2 Windows 작업 스케줄러 등록.

  실행:  powershell -ExecutionPolicy Bypass -File windows\register_tasks.ps1
  해제:  powershell -ExecutionPolicy Bypass -File windows\register_tasks.ps1 -Remove

  설계 근거 (§11.1 "켜서 로그온하면 실행")
    - 상시 가동은 필요 없다. 페이퍼 체결가는 "다음날 시가"라는 과거 확정값이라
      노트북이 5일 꺼져 있다가 켜져도 --catchup 이 5일치를 순서대로 처리하고
      결과는 매일 돌린 것과 같다. 잃는 것은 리포트 도착 시각뿐이다.
    - 모든 작업에 "놓친 실행 즉시 시작" ON, "AC 전원 연결 시에만" OFF.
    - daily/weekly/monthly/healthcheck 는 로그온 여부와 무관하게 실행.
    - evolve 만 "로그온 시에만 실행" — Claude Code 인증 토큰이 로그온 세션에 있다.
#>

param(
    [switch]$Remove,
    [string]$RepoRoot = "C:\dev\quant"
)

$ErrorActionPreference = "Stop"
$Prefix = "quant-"

$Tasks = @(
    @{ Name = "daily-logon";  Trigger = "logon";              Cmd = "run_daily.cmd --catchup";     Interactive = $false }
    @{ Name = "daily-kr";     Trigger = "weekly-16:30-MON,TUE,WED,THU,FRI"; Cmd = "run_daily.cmd --market kr"; Interactive = $false }
    @{ Name = "daily-us";     Trigger = "weekly-07:00-TUE,WED,THU,FRI,SAT"; Cmd = "run_daily.cmd --market us"; Interactive = $false }
    @{ Name = "evolve";       Trigger = "daily-19:00";        Cmd = "run_evolve.cmd";              Interactive = $true  }
    @{ Name = "weekly";       Trigger = "weekly-09:00-SAT";   Cmd = "run_task.cmd weekly";         Interactive = $false }
    @{ Name = "monthly";      Trigger = "monthly-1-09:00";    Cmd = "run_task.cmd monthly";        Interactive = $false }
    @{ Name = "healthcheck";  Trigger = "hourly";             Cmd = "run_task.cmd healthcheck";    Interactive = $false }
)

function Remove-Tasks {
    foreach ($t in $Tasks) {
        $name = "$Prefix$($t.Name)"
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "삭제: $name"
        }
    }
}

function New-Trigger([string]$spec) {
    switch -Regex ($spec) {
        '^logon$'                 { return New-ScheduledTaskTrigger -AtLogOn }
        '^hourly$'                { return New-ScheduledTaskTrigger -Once -At (Get-Date) `
                                        -RepetitionInterval (New-TimeSpan -Hours 1) }
        '^daily-(\d\d:\d\d)$'     { return New-ScheduledTaskTrigger -Daily -At $Matches[1] }
        '^weekly-(\d\d:\d\d)-(.+)$' {
            $days = $Matches[2] -split ','
            return New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $Matches[1]
        }
        '^monthly-(\d+)-(\d\d:\d\d)$' {
            # PowerShell 기본 cmdlet 에 월간 트리거가 없어 schtasks 로 대체한다.
            return $null
        }
        default { throw "알 수 없는 트리거: $spec" }
    }
}

if ($Remove) { Remove-Tasks; exit 0 }

if (-not (Test-Path $RepoRoot)) {
    throw "저장소를 찾을 수 없습니다: $RepoRoot  (§11.8 1번: OneDrive·Documents 밖이어야 합니다)"
}
# #34 동기화 폴더 안이면 등록 자체를 거부한다.
foreach ($frag in @("OneDrive", "Documents", "Desktop", "Dropbox", "Google Drive")) {
    if ($RepoRoot -like "*\$frag\*") {
        throw "저장소가 동기화/계정 폴더 안에 있습니다 ('$frag'). C:\dev\quant 로 옮기십시오 (#34)."
    }
}

Remove-Tasks

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

foreach ($t in $Tasks) {
    $name   = "$Prefix$($t.Name)"
    $script = Join-Path $RepoRoot "windows\$($t.Cmd.Split(' ')[0])"
    $args   = ($t.Cmd -split ' ', 2)[1]
    $action = New-ScheduledTaskAction -Execute $script -Argument $args -WorkingDirectory $RepoRoot

    if ($t.Trigger -match '^monthly-(\d+)-(\d\d:\d\d)$') {
        # 월간은 schtasks 로 등록
        $day  = $Matches[1]
        $time = $Matches[2]
        $cmd  = "`"$script`" $args"
        schtasks /Create /TN $name /TR $cmd /SC MONTHLY /D $day /ST $time /F | Out-Null
        Write-Host "등록(schtasks): $name — 매월 $day 일 $time"
        continue
    }

    $trigger = New-Trigger $t.Trigger
    $principal = if ($t.Interactive) {
        # evolve 는 로그온 세션에서만. Claude Code 인증 토큰 때문이다.
        New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    } else {
        New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
    }

    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal | Out-Null
    Write-Host "등록: $name — $($t.Trigger)$(if ($t.Interactive) { ' (로그온 시에만)' })"
}

Write-Host ""
Write-Host "등록 완료. 확인:  Get-ScheduledTask -TaskName 'quant-*'"
Write-Host "가드 설정을 먼저 채우십시오:"
Write-Host "  quant healthcheck --show-host      → config\allowed_hosts.yaml"
Write-Host "  quant healthcheck --show-network   → config\allowed_networks.yaml"
