param(
    [string]$DataDir,
    [string]$RuntimeDir,
    [string]$Destination = [Environment]::GetFolderPath('Desktop'),
    [string]$ProfileDir
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'launcher-profile.ps1')
$saved = Get-LauncherSettings $PSScriptRoot $ProfileDir
$explicitDataDir = $PSBoundParameters.ContainsKey('DataDir')
if (-not $PSBoundParameters.ContainsKey('Destination') -and $saved -and $saved.shortcut_dir) {
    $Destination = $saved.shortcut_dir
}
if (-not $DataDir -and $saved) { $DataDir = $saved.data_dir }
if (-not $RuntimeDir -and $saved) { $RuntimeDir = $saved.runtime_dir }
if (-not $DataDir) { $DataDir = Join-Path $env:LOCALAPPDATA 'CreatorArchive/workspace' }
if (-not $RuntimeDir) { $RuntimeDir = Join-Path $env:LOCALAPPDATA 'CreatorArchive/runtime' }
foreach ($value in @($DataDir, $RuntimeDir, $Destination)) {
    if (-not [IO.Path]::IsPathRooted($value)) { throw "Use absolute paths for data, runtime and shortcut destination: $value" }
}
$DataDir = [IO.Path]::GetFullPath($DataDir)
$RuntimeDir = [IO.Path]::GetFullPath($RuntimeDir)
$Destination = [IO.Path]::GetFullPath($Destination)
$environment = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '.venv')).TrimEnd('\') + '\'
if ($DataDir -eq $environment.TrimEnd('\') -or
    $RuntimeDir -eq $environment.TrimEnd('\') -or
    $DataDir.StartsWith($environment, [StringComparison]::OrdinalIgnoreCase) -or
    $RuntimeDir.StartsWith($environment, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Data and runtime directories cannot be inside .venv; nothing was changed.'
}
$pythonExe = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { $pythonExe = 'python' }
& $pythonExe -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required. Install Python, then run install.cmd again; saved data was not changed.' }
foreach ($name in @('start.ps1', 'stop.ps1', 'files.ps1', 'requirements.lock')) {
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $name))) { throw "Missing application file: $name" }
}
if ((Test-Path -LiteralPath $DataDir) -and -not (Test-Path -LiteralPath $DataDir -PathType Container)) {
    throw "Data path is not a folder: $DataDir"
}
if ($saved -and -not $explicitDataDir -and -not (Test-Path -LiteralPath $DataDir -PathType Container)) {
    throw "Saved workspace is unavailable: $DataDir. Restore it or explicitly select a workspace; nothing was changed."
}
if ((Test-Path -LiteralPath $DataDir) -and (Test-Path -LiteralPath (Join-Path $DataDir 'folders.json'))) {
    $folders = Get-Content -LiteralPath (Join-Path $DataDir 'folders.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($folders.version -ne 1 -or -not (Test-Path -LiteralPath $folders.archive_dir -PathType Container)) {
        throw "Configured archive is unavailable. Restore it before installing: $($folders.archive_dir)"
    }
}
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
$shell = New-Object -ComObject WScript.Shell
$powershell = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
$profileRoot = Split-Path -Parent (Get-LauncherProfilePath $PSScriptRoot $ProfileDir)
$specs = @{}
foreach ($action in @('Start', 'Stop', 'Files')) {
    $script = Join-Path $PSScriptRoot ($action.ToLowerInvariant() + '.ps1')
    $specs[$action] = '-NoProfile -ExecutionPolicy Bypass -File "' + $script + '" -ProfileDir "' + $profileRoot + '"'
    $linkPath = Join-Path $Destination "Creator Archive - $action.lnk"
    if (Test-Path -LiteralPath $linkPath) {
        $existing = $shell.CreateShortcut($linkPath)
        $oldOwned = $existing.TargetPath -eq $powershell -and
            $existing.Arguments.Contains('-File "' + $script + '"')
        if ($action -eq 'Files') { $oldOwned = $existing.TargetPath -eq (Join-Path $DataDir 'archive') -or $oldOwned }
        if (-not $oldOwned) { throw "Shortcut conflict: $linkPath. Existing file was retained." }
    }
}
$profilePath = Save-LauncherSettings $PSScriptRoot $ProfileDir $DataDir $RuntimeDir $Destination
foreach ($action in @('Start', 'Stop', 'Files')) {
    $link = $shell.CreateShortcut((Join-Path $Destination "Creator Archive - $action.lnk"))
    $link.TargetPath = $powershell
    $link.Arguments = $specs[$action]
    $link.WorkingDirectory = $PSScriptRoot
    $link.Description = 'Creator Archive local workspace; saved data is retained.'
    $link.Save()
}
Write-Host "Installed desktop launchers in $Destination"
Write-Host "Data: $DataDir"
Write-Host "Profile: $profilePath (retained on uninstall for reconnecting data)"
Write-Host 'Double-click Creator Archive - Start; the first start prepares pinned dependencies.'
