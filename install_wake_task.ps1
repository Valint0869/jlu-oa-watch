# 注册「唤醒后补跑一次」的计划任务。
# 触发条件：系统从睡眠/休眠唤醒（Power-Troubleshooter 事件 ID 1），延迟 30 秒等网络就绪。
# 与 30 分钟常规任务互不干扰；--if-stale 10 保证刚跑过就不会重复推送。
# 运行：powershell -ExecutionPolicy Bypass -File .\install_wake_task.ps1
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $root "oa_watch.py"
if (-not (Test-Path $script)) { throw "oa_watch.py not found: $script" }

$pythonCandidates = @(
    (Join-Path $env:USERPROFILE ".dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\pythonw.exe"),
    (Get-Command pythonw.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue)
) | Where-Object { $_ -and (Test-Path $_) }

if (-not $pythonCandidates -or @($pythonCandidates).Count -eq 0) {
    throw "pythonw.exe not found. Install Python or adjust the path in install_wake_task.ps1."
}
# 只有一个候选时管道会退化成字符串，取 [0] 会拿到首字母，所以统一用 @() 包一层。
$python = @($pythonCandidates)[0]
if (-not $python -or -not (Test-Path $python)) { throw "invalid pythonw path: $python" }

$taskName = "JLU-OA-Watch-OnWake"
# 直接调用 pythonw，不经 wscript：少一层中间进程，排查起来也直观。
$action = New-ScheduledTaskAction -Execute $python -Argument "`"$script`" --quiet --if-stale 10" -WorkingDirectory $root

$class = Get-CimClass -ClassName MSFT_TaskEventTrigger -Namespace Root/Microsoft/Windows/TaskScheduler
$trigger = New-CimInstance -CimClass $class -ClientOnly
$trigger.Enabled = $true
$trigger.Subscription = '<QueryList><Query Id="0" Path="System"><Select Path="System">*[System[Provider[@Name=''Microsoft-Windows-Power-Troubleshooter''] and (EventID=1)]]</Select></Query></QueryList>'
$trigger.Delay = "PT30S"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Run the JLU OA watcher once after the PC wakes from sleep (skipped if a run happened in the last 10 minutes)." `
    -Force | Out-Null

Write-Host "Installed: $taskName"
Write-Host "Test now:  schtasks /Run /TN $taskName"
