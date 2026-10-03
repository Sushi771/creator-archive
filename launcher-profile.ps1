function Get-LauncherProfilePath {
    param([string]$AppDir, [string]$ProfileDir)
    if (-not $ProfileDir) { $ProfileDir = Join-Path $env:LOCALAPPDATA 'CreatorArchive/launchers' }
    $bytes = [Text.Encoding]::UTF8.GetBytes([IO.Path]::GetFullPath($AppDir).TrimEnd('\').ToLowerInvariant())
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $name = [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '').Substring(0, 20) }
    finally { $sha.Dispose() }
    return Join-Path $ProfileDir "$name.json"
}

function Get-LauncherSettings {
    param([string]$AppDir, [string]$ProfileDir)
    $path = Get-LauncherProfilePath $AppDir $ProfileDir
    if (Test-Path -LiteralPath $path) {
        $saved = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($saved.version -ne 1 -or $saved.app_dir -ne $AppDir) { throw "Launcher profile does not match this app: $path" }
        return $saved
    }
    $legacy = Join-Path $AppDir '.venv/creator-archive-launcher.json'
    if (Test-Path -LiteralPath $legacy) { return Get-Content -LiteralPath $legacy -Raw -Encoding UTF8 | ConvertFrom-Json }
    return $null
}

function Save-LauncherSettings {
    param([string]$AppDir, [string]$ProfileDir, [string]$DataDir, [string]$RuntimeDir, [string]$ShortcutDir)
    $path = Get-LauncherProfilePath $AppDir $ProfileDir
    $parent = Split-Path -Parent $path
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    if (-not $ShortcutDir -and (Test-Path -LiteralPath $path)) {
        $previous = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
        $ShortcutDir = $previous.shortcut_dir
    }
    $temporary = Join-Path $parent ('.launcher-' + [guid]::NewGuid().ToString('N') + '.tmp')
    $backup = Join-Path $parent ('.launcher-' + [guid]::NewGuid().ToString('N') + '.bak')
    try {
        @{ version = 1; app_dir = $AppDir; data_dir = $DataDir; runtime_dir = $RuntimeDir; shortcut_dir = $ShortcutDir } |
            ConvertTo-Json | Set-Content -LiteralPath $temporary -Encoding UTF8
        if (Test-Path -LiteralPath $path) { [IO.File]::Replace($temporary, $path, $backup) }
        else { [IO.File]::Move($temporary, $path) }
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary }
        if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup }
    }
    return $path
}
