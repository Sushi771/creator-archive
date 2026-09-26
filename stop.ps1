param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$stateFile = Join-Path $env:LOCALAPPDATA "CreatorArchive/runtime/server-$Port.json"
if (-not (Test-Path -LiteralPath $stateFile)) { Write-Host 'No managed server is recorded.'; return }
$saved = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
if ($saved.workspace -ne $PSScriptRoot) { throw "This server belongs to $($saved.workspace). Use that checkout to stop it." }
$server = Get-Process -Id $saved.pid -ErrorAction SilentlyContinue
if ($server) {
    if ($server.Path -ne $saved.executable -or $server.StartTime.ToUniversalTime().Ticks.ToString() -ne $saved.start_ticks) {
        throw 'The recorded PID now belongs to a different process; nothing was stopped.'
    }
    try {
        Invoke-RestMethod -Method Post -Uri ($saved.url + 'api/server/stop') -Headers @{'X-Creator-Archive'='local-validation'} -TimeoutSec 5 | Out-Null
    } catch {
        throw 'The managed server did not accept a graceful stop. It was left running; inspect the runtime log before retrying.'
    }
    if (-not $server.WaitForExit(40000)) {
        throw 'The server is still closing its browser or current operation. Data is retained; wait briefly and run stop.cmd again.'
    }
}
Remove-Item -LiteralPath $stateFile
Write-Host 'Creator Archive stopped. Saved files and checkpoints are retained; interrupted jobs can be resumed after restart.'
