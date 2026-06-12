$Log = Join-Path $env:LOCALAPPDATA "HomeTwin\logs\hometwin.log"
if (-not (Test-Path $Log)) { Write-Host "no log yet at $Log"; exit 0 }
Get-Content $Log -Tail 50 -Wait
