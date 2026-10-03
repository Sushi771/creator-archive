param([string]$ProfileDir, [switch]$KeepEnvironment)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'launcher-profile.ps1')
$profilePath = Get-LauncherProfilePath $PSScriptRoot $ProfileDir
if (-not (Test-Path -LiteralPath $profilePath)) {
    throw "No installation profile for this checkout: $profilePath. Nothing was removed."
}
$settings = Get-LauncherSettings $PSScriptRoot $ProfileDir
$destination = $settings.shortcut_dir
if (-not $destination -or -not [IO.Path]::IsPathRooted($destination)) {
    throw 'Shortcut location is missing from the installation profile; nothing was removed.'
}
$shell = New-Object -ComObject WScript.Shell
$powershell = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
$links = @()
foreach ($action in @('Start', 'Stop', 'Files')) {
    $path = Join-Path $destination "Creator Archive - $action.lnk"
    if (-not (Test-Path -LiteralPath $path)) { continue }
    $link = $shell.CreateShortcut($path)
    $script = Join-Path $PSScriptRoot ($action.ToLowerInvariant() + '.ps1')
    if ($link.TargetPath -ne $powershell -or
        -not $link.Arguments.Contains('-File "' + $script + '"')) {
        throw "Shortcut is no longer owned by this installation: $path. Nothing was removed."
    }
    $links += $path
}
$environment = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '.venv'))
$environmentPrefix = $environment.TrimEnd('\') + '\'
if (-not $KeepEnvironment) {
    foreach ($folder in @($settings.data_dir, $settings.runtime_dir)) {
        if ($folder -and ([IO.Path]::GetFullPath($folder) -eq $environment -or
            [IO.Path]::GetFullPath($folder).StartsWith($environmentPrefix, [StringComparison]::OrdinalIgnoreCase))) {
            throw "Saved data or runtime is inside .venv: $folder. Nothing was removed."
        }
    }
    if ((Test-Path -LiteralPath $environment) -and
        ((Get-Item -LiteralPath $environment -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw '.venv is a link; nothing was removed.'
    }
}
if (Test-Path -LiteralPath $settings.runtime_dir -PathType Container) {
    foreach ($state in Get-ChildItem -LiteralPath $settings.runtime_dir -Filter 'server-*.json' -File) {
        $record = Get-Content -LiteralPath $state.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($record.workspace -ne $PSScriptRoot) { continue }
        if ($state.BaseName -notmatch '^server-(\d+)$') { throw "Unknown managed server record: $($state.FullName)" }
        & (Join-Path $PSScriptRoot 'stop.ps1') -Port ([int]$Matches[1]) -RuntimeDir $settings.runtime_dir -ProfileDir (Split-Path -Parent $profilePath)
    }
}
foreach ($path in $links) { Remove-Item -LiteralPath $path }
if (-not $KeepEnvironment -and (Test-Path -LiteralPath $environment)) {
    Remove-Item -LiteralPath $environment -Recurse -Force
}
if ($KeepEnvironment) { Write-Host 'Uninstalled launchers; local Python environment was kept.' }
else { Write-Host "Uninstalled launchers and local Python environment from $PSScriptRoot" }
Write-Host "Saved workspace retained: $($settings.data_dir)"
Write-Host "Runtime location retained: $($settings.runtime_dir)"
Write-Host "Reconnect profile retained: $profilePath"
Write-Host 'Run install.cmd from this same folder to reinstall without selecting the workspace again.'
