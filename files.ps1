param([string]$ProfileDir, [switch]$PrintOnly)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'launcher-profile.ps1')
$settings = Get-LauncherSettings $PSScriptRoot $ProfileDir
if (-not $settings) { throw 'No installed workspace is recorded. Run install.cmd or start.cmd first.' }
$dataDir = $settings.data_dir
$archiveDir = Join-Path $dataDir 'archive'
$folderConfig = Join-Path $dataDir 'folders.json'
if (Test-Path -LiteralPath $folderConfig) {
    $folders = Get-Content -LiteralPath $folderConfig -Raw | ConvertFrom-Json
    if ($folders.version -ne 1) { throw "Unsupported folder settings: $folderConfig" }
    $archiveDir = $folders.archive_dir
}
if (-not (Test-Path -LiteralPath $archiveDir -PathType Container)) {
    throw "Archive folder is unavailable: $archiveDir. Saved data was not changed; restore the folder or update folder settings."
}
Write-Host "Archive: $archiveDir"
if (-not $PrintOnly) { Invoke-Item -LiteralPath $archiveDir }
