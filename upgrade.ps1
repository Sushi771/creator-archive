param(
    [Parameter(Mandatory=$true)][string]$PreviousInstall,
    [string]$PreviousProfileDir,
    [int]$Port = 8765
)
$ErrorActionPreference = 'Stop'
$old = (Resolve-Path -LiteralPath $PreviousInstall -ErrorAction Stop).Path
$new = [IO.Path]::GetFullPath($PSScriptRoot)
if ($old.TrimEnd('\') -eq $new.TrimEnd('\')) { throw 'Extract the new release to a different program directory.' }
. (Join-Path $old 'launcher-profile.ps1')
$saved = Get-LauncherSettings $old $PreviousProfileDir
if (-not $saved) { throw 'Previous installation profile is missing. No data or shortcuts were changed.' }
$shortcutDir = if ($saved.shortcut_dir) { $saved.shortcut_dir } else { [Environment]::GetFolderPath('Desktop') }
foreach ($path in @($saved.data_dir, $saved.runtime_dir, $shortcutDir)) {
    if (-not [IO.Path]::IsPathRooted($path)) { throw "Invalid previous installation path: $path" }
}
if (-not (Test-Path -LiteralPath $saved.data_dir -PathType Container)) {
    throw "Previous workspace is unavailable: $($saved.data_dir)"
}
$shell = New-Object -ComObject WScript.Shell
$powershell = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
$shortcuts = @('Start','Stop','Files')
foreach ($action in $shortcuts) {
    $linkPath = Join-Path $shortcutDir "Creator Archive - $action.lnk"
    if (Test-Path -LiteralPath $linkPath) {
        $link = $shell.CreateShortcut($linkPath)
        $oldScript = Join-Path $old ($action.ToLowerInvariant() + '.ps1')
        $legacyFiles = $action -eq 'Files' -and $link.TargetPath -eq (Join-Path $saved.data_dir 'archive')
        if (-not $legacyFiles -and ($link.TargetPath -ne $powershell -or -not $link.Arguments.Contains('-File "' + $oldScript + '"'))) {
            throw "Shortcut does not belong to previous installation: $linkPath. Nothing was changed."
        }
    }
}
$profileRoot = if ($PreviousProfileDir) { $PreviousProfileDir } else { Join-Path $env:LOCALAPPDATA 'CreatorArchive/launchers' }
$oldState = Join-Path $saved.runtime_dir "server-$Port.json"
$wasRunning = Test-Path -LiteralPath $oldState
if (-not $wasRunning) {
    $occupied = [System.Net.Sockets.TcpClient]::new()
    try {
        $probe = $occupied.ConnectAsync('127.0.0.1', $Port)
        if ($probe.Wait(600) -and $occupied.Connected) {
            throw "Port $Port is occupied but no previous service is recorded in $($saved.runtime_dir). Stop its owner or repair the saved runtime path before upgrading; nothing was changed."
        }
    } finally { $occupied.Dispose() }
}
$backupDir = Join-Path $saved.data_dir ('backups/before-release-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8))
$backupMade = $false
$newInstalled = $false
$shortcutsTouched = $false
try {
    & (Join-Path $old 'stop.ps1') -Port $Port -RuntimeDir $saved.runtime_dir -ProfileDir $profileRoot
    if (-not $?) { throw 'Previous service did not stop cleanly.' }
    Set-Location -LiteralPath $new
    & python -m creator_archive.release_backup $saved.data_dir $backupDir
    if ($LASTEXITCODE -ne 0) { throw 'Workspace backup failed. Previous program can be started again.' }
    $backupMade = $true
    $linkBackupDir = Join-Path $backupDir 'shortcuts'
    New-Item -ItemType Directory -Path $linkBackupDir -Force | Out-Null
    foreach ($action in $shortcuts) {
        $linkPath = Join-Path $shortcutDir "Creator Archive - $action.lnk"
        if (Test-Path -LiteralPath $linkPath) {
            Copy-Item -LiteralPath $linkPath -Destination (Join-Path $linkBackupDir "Creator Archive - $action.lnk")
            Remove-Item -LiteralPath $linkPath
            $shortcutsTouched = $true
        }
    }
    $newInstalled = $true
    & (Join-Path $new 'install.ps1') -DataDir $saved.data_dir -RuntimeDir $saved.runtime_dir -Destination $shortcutDir -ProfileDir $profileRoot
    if (-not $?) { throw 'New launchers could not be installed.' }
    & (Join-Path $new 'start.ps1') -Port $Port -NoBrowser -DataDir $saved.data_dir -RuntimeDir $saved.runtime_dir -ProfileDir $profileRoot
    if (-not $?) { throw 'New service failed to start.' }
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/status" -TimeoutSec 5
    $releasePath = Join-Path $new 'release-info.json'
    if (Test-Path -LiteralPath $releasePath) {
        $release = Get-Content -LiteralPath $releasePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($health.commit -ne $release.commit -or $health.version -ne $release.version) {
            throw 'Running service does not match the new release manifest.'
        }
    }
    Write-Host "Upgrade ready: http://127.0.0.1:$Port/"
    Write-Host "Workspace backup: $backupDir"
    Write-Host "Previous program directory remains available: $old"
} catch {
    $reason = $_.Exception.Message
    if ($newInstalled) {
        try {
            & (Join-Path $new 'stop.ps1') -Port $Port -RuntimeDir $saved.runtime_dir -ProfileDir $profileRoot
            if (-not $?) { throw 'New service stop failed.' }
        } catch {
            throw "Upgrade failed: $reason. New service could not be confirmed stopped, so the database was not overwritten. Stop it safely before restoring backup: $backupDir"
        }
    }
    $listener = [System.Net.Sockets.TcpClient]::new()
    try {
        $connection = $listener.ConnectAsync('127.0.0.1', $Port)
        if ($connection.Wait(600) -and $listener.Connected) {
            throw "Upgrade failed: $reason. Port $Port is still occupied; no database restore was attempted. Backup: $backupDir"
        }
    } finally { $listener.Dispose() }
    if ($backupMade) {
        foreach ($name in @('archive.sqlite3','state.sqlite3','folders.json','sources.json')) {
            $source = Join-Path $backupDir $name
            if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination (Join-Path $saved.data_dir $name) -Force }
        }
    }
    if ($shortcutsTouched -or $newInstalled) {
        foreach ($action in $shortcuts) {
            $linkPath = Join-Path $shortcutDir "Creator Archive - $action.lnk"
            if (Test-Path -LiteralPath $linkPath) {
                $link = $shell.CreateShortcut($linkPath)
                $newScript = Join-Path $new ($action.ToLowerInvariant() + '.ps1')
                if ($link.TargetPath -ne $powershell -or -not $link.Arguments.Contains('-File "' + $newScript + '"')) {
                    throw "Upgrade failed: $reason. Shortcut changed unexpectedly and was not overwritten: $linkPath. Backup: $backupDir"
                }
                Remove-Item -LiteralPath $linkPath
            }
            $original = Join-Path $linkBackupDir "Creator Archive - $action.lnk"
            if (Test-Path -LiteralPath $original) { Copy-Item -LiteralPath $original -Destination $linkPath }
        }
    }
    if ($wasRunning) { & (Join-Path $old 'start.ps1') -Port $Port -NoBrowser -DataDir $saved.data_dir -RuntimeDir $saved.runtime_dir -ProfileDir $profileRoot }
    throw "Upgrade failed and previous launchers were restored: $reason. Backup: $backupDir"
}
