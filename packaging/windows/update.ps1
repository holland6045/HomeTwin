# Manual update: force-reinstall from the configured channel and stop the
# tracker so the next launch runs the new code.
$ErrorActionPreference = "Stop"
$RepoUrl = if ($env:HOMETWIN_REPO) { $env:HOMETWIN_REPO } else { "__REPO_URL__" }
$Support = Join-Path $env:LOCALAPPDATA "HomeTwin"
$ChannelFile = Join-Path $Support "channel"
$Channel = if (Test-Path $ChannelFile) { (Get-Content $ChannelFile -Raw).Trim() } else { "__CHANNEL__" }

Write-Host "updating hometwin from $Channel ..."
& (Join-Path $Support "venv\Scripts\python.exe") -m pip install --upgrade --force-reinstall `
    "hometwin[vision] @ git+$RepoUrl@$Channel"
if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed"; exit 1 }

$line = git ls-remote $RepoUrl "refs/heads/$Channel" | Select-Object -First 1
if ($line) { Set-Content (Join-Path $Support "installed-commit") (($line -split "\s+")[0]) }

$PidFile = Join-Path $Support "hometwin.pid"
if (Test-Path $PidFile) {
    Stop-Process -Id (Get-Content $PidFile) -Force -ErrorAction SilentlyContinue
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    Write-Host "tracker stopped - relaunch HomeTwin to start the new version"
}
Write-Host "done. installed: $(Get-Content (Join-Path $Support 'installed-commit') -ErrorAction SilentlyContinue)"
