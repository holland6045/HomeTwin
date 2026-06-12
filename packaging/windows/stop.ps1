$Support = Join-Path $env:LOCALAPPDATA "HomeTwin"
$PidFile = Join-Path $Support "hometwin.pid"
if (Test-Path $PidFile) {
    Stop-Process -Id (Get-Content $PidFile) -Force -ErrorAction SilentlyContinue
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    Write-Host "tracker stopped"
} else {
    $procs = Get-Process hometwin -ErrorAction SilentlyContinue
    if ($procs) { $procs | Stop-Process -Force; Write-Host "tracker stopped" }
    else { Write-Host "not running" }
}
