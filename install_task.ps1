# Install the JLU OA watcher as a hidden Windows scheduled task.
# Run: powershell -ExecutionPolicy Bypass -File .\install_task.ps1
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $root "oa_watch.py"
if (-not (Test-Path $script)) { throw "oa_watch.py not found: $script" }

$pythonCandidates = @(
    (Join-Path $env:USERPROFILE ".dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\pythonw.exe"),
    (Get-Command pythonw.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue)
) | Where-Object { $_ -and (Test-Path $_) }

if (-not $pythonCandidates -or $pythonCandidates.Count -eq 0) {
    throw "pythonw.exe not found. Install Python or adjust the path in install_task.ps1."
}
$python = $pythonCandidates[0]

$vbs = Join-Path $root "run_hidden.vbs"
$vbsContent = @"
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "$root"
sh.Run """$python"" ""$script"" --quiet", 0, False
"@
Set-Content -Path $vbs -Value $vbsContent -Encoding ASCII

$taskName = "JLU-OA-Watch"
$action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\wscript.exe" -Argument "`"$vbs`""
$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(2)) -RepetitionInterval (New-TimeSpan -Minutes 30)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Read JLU OA public notices every 30 minutes and push relevant ones to WeChat." `
    -Force | Out-Null

Write-Host "Installed scheduled task: $taskName"
Write-Host "Runs every 30 minutes while the user is logged on; quiet hours 23:30-07:00."
Write-Host "Test now:  schtasks /Run /TN $taskName"
Write-Host "Logs:      $root\logs\oa_watch.log"
