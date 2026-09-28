param([string]$Destination)
$ErrorActionPreference = 'Stop'
if ($Destination) { & (Join-Path $PSScriptRoot 'install.ps1') -Destination $Destination }
else { & (Join-Path $PSScriptRoot 'install.ps1') }
