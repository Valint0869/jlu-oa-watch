# Remove the JLU OA watcher scheduled tasks (regular + wake).
$ErrorActionPreference = "Stop"
foreach ($taskName in @("JLU-OA-Watch", "JLU-OA-Watch-OnWake")) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task: $taskName (if it existed)."
}
