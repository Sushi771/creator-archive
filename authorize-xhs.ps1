param([string]$ProfileDir, [ValidateSet('msedge','chrome')][string]$Channel = 'msedge', [switch]$PreflightOnly)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'launcher-profile.ps1')

$roots = @()
if ($ProfileDir) {
    $roots = @($ProfileDir)
} else {
    $roots += Join-Path $env:LOCALAPPDATA 'CreatorArchive\launchers'
    $packages = Join-Path $env:LOCALAPPDATA 'Packages'
    if (Test-Path -LiteralPath $packages -PathType Container) {
        Get-ChildItem -LiteralPath $packages -Directory -Filter 'OpenAI.Codex_*' | ForEach-Object {
            $roots += Join-Path $_.FullName 'LocalCache\Local\CreatorArchive\launchers'
        }
    }
}
$matches = @()
foreach ($root in ($roots | Select-Object -Unique)) {
    $saved = Get-LauncherSettings $PSScriptRoot $root
    if ($saved) { $matches += $saved }
}
if ($matches.Count -ne 1) {
    throw 'Cannot identify exactly one installed Creator Archive workspace. Run with -ProfileDir set to the saved launcher directory; no new database was created.'
}
$dataDir = [IO.Path]::GetFullPath($matches[0].data_dir)
if (-not (Test-Path -LiteralPath (Join-Path $dataDir 'archive.sqlite3') -PathType Leaf)) {
    throw 'Saved Creator Archive database is unavailable. Restore the existing workspace before authorising.'
}
$pythonExe = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw 'Start the installed Creator Archive once to prepare its pinned Python environment.'
}
$cookieFile = Join-Path $dataDir 'private\xhs-session.cookie'
if ($PreflightOnly) {
    Write-Host 'Installed application, saved workspace and Python runtime are ready for the one-time XHS login.'
    return
}
Push-Location -LiteralPath $PSScriptRoot
try {
    & $pythonExe -m creator_archive.adapters.xhs_http --cookie-file $cookieFile --channel $Channel
    if ($LASTEXITCODE -ne 0) { throw '本人登录尚未完成；旧来源配置未改动。' }
    & $pythonExe -m creator_archive.xhs_source_setup $dataDir $cookieFile
    if ($LASTEXITCODE -ne 0) { throw '会话已保存在本机，但来源配置未完成；旧配置和资料未覆盖。' }
    Write-Host '请回到 Creator Archive 粘贴已确认博主链接并核验，再开始历史同步。'
} finally {
    Pop-Location
}
