param(
    [int]$Port = 8765,
    [switch]$NoBrowser,
    [switch]$Foreground,
    [string]$DataDir,
    [string]$RuntimeDir,
    [string]$ProfileDir
)
$ErrorActionPreference = 'Stop'
# Start only the local application; platform requests require a user action.

Set-Location -LiteralPath $PSScriptRoot
. (Join-Path $PSScriptRoot 'launcher-profile.ps1')
$config = Get-LauncherSettings $PSScriptRoot $ProfileDir
$explicitDataDir = $PSBoundParameters.ContainsKey('DataDir')
if ($config) {
    if (-not $DataDir) { $DataDir = $config.data_dir }
    if (-not $RuntimeDir) { $RuntimeDir = $config.runtime_dir }
}
if (-not $DataDir) { $DataDir = Join-Path $env:LOCALAPPDATA 'CreatorArchive/workspace' }
if (-not $RuntimeDir) { $RuntimeDir = Join-Path $env:LOCALAPPDATA 'CreatorArchive/runtime' }
if ($config -and -not $explicitDataDir -and -not (Test-Path -LiteralPath $DataDir -PathType Container)) {
    throw "Saved workspace is unavailable: $DataDir. Restore it or explicitly select a workspace; no new database was created."
}
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
$stateFile = Join-Path $runtimeDir "server-$Port.json"
$pythonExe = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
$url = "http://127.0.0.1:$Port/"
$launchLock = [System.IO.File]::Open((Join-Path $runtimeDir "launch-$Port.lock"), 'OpenOrCreate', 'ReadWrite', 'None')
try {
    if (Test-Path -LiteralPath $stateFile) {
        $saved = Get-Content -LiteralPath $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $existing = Get-Process -Id $saved.pid -ErrorAction SilentlyContinue
        if ($existing -and $existing.Path -eq $saved.executable -and $existing.StartTime.ToUniversalTime().Ticks.ToString() -eq $saved.start_ticks) {
            if ($saved.workspace -ne $PSScriptRoot) { throw "This port belongs to another Creator Archive checkout: $($saved.workspace)" }
            try {
                $health = Invoke-RestMethod -Uri ($url + 'api/status') -TimeoutSec 4
                if ($health.mode -ne 'local_mvp') { throw 'Unexpected service' }
                $workspace = Invoke-RestMethod -Uri ($url + 'api/workspace') -TimeoutSec 4
                if ($workspace.data_dir -ne $saved.data_dir) { throw 'Unexpected data directory' }
            } catch { throw "The recorded server is not responding correctly. Progress is retained. Run stop.cmd, then start.cmd; inspect $runtimeDir if this persists." }
            Write-Host "Creator Archive is already running: $url"
            if (-not $NoBrowser) { Start-Process $url }
            return
        }
    }
    $listener = [System.Net.Sockets.TcpClient]::new()
    try {
        $connection = $listener.ConnectAsync('127.0.0.1', $Port)
        if ($connection.Wait(600) -and $listener.Connected) { throw "Port $Port is occupied. Stop its owner or choose -Port; no existing process was changed." }
    } catch [System.AggregateException] { } finally { $listener.Dispose() }
    if (-not (Test-Path -LiteralPath $pythonExe)) {
        Write-Host 'Creating the local Python environment...'
        & python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required; environment creation failed.' }
    }
    $hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        $lockHash = [BitConverter]::ToString($hasher.ComputeHash([System.IO.File]::ReadAllBytes((Join-Path $PSScriptRoot 'requirements.lock')))).Replace('-', '')
    } finally { $hasher.Dispose() }
    $dependencyStamp = Join-Path $PSScriptRoot '.venv/creator-archive-requirements.txt'
    if (-not (Test-Path -LiteralPath $dependencyStamp) -or (Get-Content -LiteralPath $dependencyStamp -Raw).Trim() -ne $lockHash) {
        Write-Host 'Installing pinned application dependencies...'
        & $pythonExe -m pip install --disable-pip-version-check -r requirements.lock
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check the network and run start.cmd again.' }
        Set-Content -LiteralPath $dependencyStamp -Value $lockHash -Encoding ascii
    }
    if ($DataDir.Contains('"')) { throw 'DataDir must not contain quotation marks.' }
    if ($Foreground) {
        Write-Host "Creator Archive: $url (Ctrl+C to stop)"
        & $pythonExe -X utf8 -m creator_archive --port $Port --data-dir $DataDir
        return
    }
    # Windows PowerShell 5 Start-Process can fail when inherited Path/PATH keys collide.
    $serverPid = & $pythonExe -m creator_archive.launcher_spawn $Port $DataDir $PSScriptRoot (Join-Path $runtimeDir "server-$Port.stdout.log") (Join-Path $runtimeDir "server-$Port.stderr.log")
    if ($LASTEXITCODE -ne 0 -or -not $serverPid) { throw 'Could not launch the server. Saved data was not removed.' }
    $server = Get-Process -Id ([int]$serverPid) -ErrorAction Stop
    $server.Refresh()
    @{ pid = $server.Id; executable = $pythonExe; start_ticks = $server.StartTime.ToUniversalTime().Ticks.ToString(); workspace = $PSScriptRoot; url = $url; data_dir = $DataDir } | ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding utf8
    $ready = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        if ($server.HasExited) { throw "Server exited. See $runtimeDir/server-$Port.stderr.log" }
        try {
            $health = Invoke-RestMethod -Uri ($url + 'api/status') -TimeoutSec 2
            if ($health.version) { $ready = $true; break }
        } catch { Start-Sleep -Milliseconds 250 }
    }
    if (-not $ready) { throw "Server is not ready. See $runtimeDir/server-$Port.stderr.log; stop.cmd safely stops this launch." }
    $workspace = Invoke-RestMethod -Uri ($url + 'api/workspace') -TimeoutSec 4
    $DataDir = $workspace.data_dir
    # Keep Unicode paths inside PowerShell. Python stdout can be decoded with
    # the console codepage and corrupt Chinese runtime paths in the profile.
    $canonicalRuntime = [IO.Path]::GetFullPath($runtimeDir)
    Save-LauncherSettings $PSScriptRoot $ProfileDir $DataDir $canonicalRuntime | Out-Null
    @{ pid = $server.Id; executable = $pythonExe; start_ticks = $server.StartTime.ToUniversalTime().Ticks.ToString(); workspace = $PSScriptRoot; url = $url; data_dir = $DataDir } | ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding utf8
    Write-Host "Creator Archive is ready: $url"
    Write-Host "Data: $DataDir"
    Write-Host 'Stop with stop.cmd. Your archive and checkpoints are retained.'
    if (-not $NoBrowser) { Start-Process $url }
} finally { $launchLock.Dispose() }
