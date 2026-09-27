param([string]$Destination = [Environment]::GetFolderPath('Desktop'))
$ErrorActionPreference = 'Stop'
$configFile = Join-Path $PSScriptRoot '.venv/creator-archive-launcher.json'
if (-not (Test-Path -LiteralPath $configFile)) { throw 'Run start.cmd successfully once before creating shortcuts.' }
$config = Get-Content -LiteralPath $configFile -Raw | ConvertFrom-Json
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
$shell = New-Object -ComObject WScript.Shell
foreach ($action in @('Start', 'Stop')) {
    $linkPath = Join-Path $Destination "Creator Archive - $action.lnk"
    if (Test-Path -LiteralPath $linkPath) { throw "Shortcut already exists: $linkPath. Existing files were retained." }
    $link = $shell.CreateShortcut($linkPath)
    $link.TargetPath = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
    $script = Join-Path $PSScriptRoot ($action.ToLower() + '.ps1')
    $link.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $script + '" -RuntimeDir "' + $config.runtime_dir + '"'
    if ($action -eq 'Start') { $link.Arguments += ' -DataDir "' + $config.data_dir + '"' }
    $link.WorkingDirectory = $PSScriptRoot
    $link.Description = 'Creator Archive: local workspace; saved files and progress are retained.'
    $link.Save()
}
$archiveLink = Join-Path $Destination 'Creator Archive - Files.lnk'
if (-not (Test-Path -LiteralPath $archiveLink)) {
    $link = $shell.CreateShortcut($archiveLink)
    $link.TargetPath = Join-Path $config.data_dir 'archive'
    $link.Save()
}
Write-Host "Shortcuts created in $Destination"
Write-Host "Data directory pinned to $($config.data_dir)"
