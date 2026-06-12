# HomeTwin launcher: owns a venv under %LOCALAPPDATA%\HomeTwin, self-updates
# from the configured git channel, starts the tracker, opens the dashboard.
# Relaunching IS the update - the same loop as HomeTwin.app on macOS.
$ErrorActionPreference = "Stop"

$RepoUrl = if ($env:HOMETWIN_REPO) { $env:HOMETWIN_REPO } else { "__REPO_URL__" }
$DefaultChannel = "__CHANNEL__"
$Port = 8080

$Support = Join-Path $env:LOCALAPPDATA "HomeTwin"
$Logs    = Join-Path $Support "logs"
$Venv    = Join-Path $Support "venv"
$Config  = Join-Path $Support "config.yaml"
$Log     = Join-Path $Logs "hometwin.log"
$Here    = Split-Path -Parent $MyInvocation.MyCommand.Path

New-Item -ItemType Directory -Force -Path $Support, $Logs | Out-Null
$ChannelFile = Join-Path $Support "channel"
$Channel = if (Test-Path $ChannelFile) { (Get-Content $ChannelFile -Raw).Trim() } else { $DefaultChannel }
Set-Content $ChannelFile $Channel

function Say($msg) { Add-Content $Log "[hometwin-app] $msg" }
function Alert($msg) {
    Add-Type -AssemblyName System.Windows.Forms
    [void][System.Windows.Forms.MessageBox]::Show($msg, "HomeTwin")
}

$Py = (Get-Command py -ErrorAction SilentlyContinue).Source
$PyArgs = @("-3")
if (-not $Py) {
    $Py = (Get-Command python -ErrorAction SilentlyContinue).Source
    $PyArgs = @()
}
if (-not $Py) {
    Alert "Python 3 not found. Install it from python.org (keep 'py launcher' checked), then relaunch."
    exit 1
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Alert "git not found. Install Git for Windows (git-scm.com), then relaunch - updates install straight from the repo."
    exit 1
}

function Remote-Commit {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $line = git ls-remote $RepoUrl "refs/heads/$Channel" 2>> $Log | Select-Object -First 1
        if ($line) { return ($line -split "\s+")[0] }
    } catch { Say "ls-remote failed: $_" }
    finally { $ErrorActionPreference = $prev }
    return $null
}

function Install-Update($target) {
    Say "installing hometwin@$Channel ($target)"
    & (Join-Path $Venv "Scripts\python.exe") -m pip install --quiet --upgrade --force-reinstall `
        "hometwin[vision] @ git+$RepoUrl@$Channel" *>> $Log
    if ($LASTEXITCODE -ne 0) { throw "pip install failed - see $Log" }
    Set-Content (Join-Path $Support "installed-commit") $target
}

function Stop-Tracker {
    $PidFile = Join-Path $Support "hometwin.pid"
    if (Test-Path $PidFile) {
        Stop-Process -Id (Get-Content $PidFile) -Force -ErrorAction SilentlyContinue
        Remove-Item $PidFile -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    }
}

$AutoFile = Join-Path $Support "autoupdate"
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
    Say "first run: creating venv"
    & $Py @PyArgs -m venv $Venv *>> $Log
    if ($LASTEXITCODE -ne 0) { Alert "Could not create the Python environment - see $Log"; exit 1 }
    & (Join-Path $Venv "Scripts\python.exe") -m pip install --quiet --upgrade pip *>> $Log
    $target = Remote-Commit
    if (-not $target) { $target = "unknown" }
    try { Install-Update $target }
    catch { Alert "First install failed - see $Log"; exit 1 }
    if (-not (Test-Path $AutoFile)) { Set-Content $AutoFile "true" }
} elseif ((Get-Content $AutoFile -ErrorAction SilentlyContinue) -eq "true") {
    $Remote = Remote-Commit
    $Local = if (Test-Path (Join-Path $Support "installed-commit")) {
        (Get-Content (Join-Path $Support "installed-commit") -Raw).Trim()
    } else { "none" }
    if ($Remote -and $Remote -ne $Local) {
        Say "update available: $Local -> $Remote"
        try { Install-Update $Remote; Stop-Tracker }  # restart so new code is live
        catch { Say "update failed, keeping $Local : $_" }
    }
}

if (-not (Test-Path $Config)) {
    Copy-Item (Join-Path $Here "default-config.yaml") $Config
    Say "wrote default config to $Config"
}

function Healthy {
    try {
        Invoke-WebRequest "http://127.0.0.1:$Port/health" -UseBasicParsing -TimeoutSec 2 | Out-Null
        return $true
    } catch { return $false }
}

if (Healthy) {
    Say "tracker already running"
} else {
    Say "starting tracker"
    $proc = Start-Process -FilePath (Join-Path $Venv "Scripts\hometwin.exe") `
        -ArgumentList "run", "-c", $Config -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $Logs "tracker.out.log") `
        -RedirectStandardError (Join-Path $Logs "tracker.err.log")
    Set-Content (Join-Path $Support "hometwin.pid") $proc.Id
    foreach ($i in 1..30) {
        if (Healthy) { break }
        Start-Sleep -Milliseconds 500
    }
    if (-not (Healthy)) {
        Alert "HomeTwin failed to start. Log: $Logs (camera permission? device index? config at $Config)"
        exit 1
    }
}

Start-Process "http://127.0.0.1:$Port/"
