param(
    [string]$DataDir,
    [string]$ArchiveDir,
    [string]$ObsidianDir,
    [string]$RuntimeDir
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$launcherConfig = Join-Path $PSScriptRoot '.venv/creator-archive-launcher.json'
if (Test-Path -LiteralPath $launcherConfig) {
    $saved = Get-Content -LiteralPath $launcherConfig -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $DataDir) { $DataDir = $saved.data_dir }
    if (-not $RuntimeDir) { $RuntimeDir = $saved.runtime_dir }
}
if (-not $DataDir) { $DataDir = Join-Path $env:LOCALAPPDATA 'CreatorArchive/workspace' }
if (-not $RuntimeDir) { $RuntimeDir = Join-Path $env:LOCALAPPDATA 'CreatorArchive/runtime' }
$pythonExe = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw '请先用 start.cmd 安装本机环境，再 stop.cmd 停止后配置目录。' }
if (Test-Path -LiteralPath $RuntimeDir) {
    foreach ($stateFile in Get-ChildItem -LiteralPath $RuntimeDir -Filter 'server-*.json' -File) {
        try {
            $state = Get-Content -LiteralPath $stateFile.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
            $process = Get-Process -Id $state.pid -ErrorAction SilentlyContinue
            if ($process -and $process.Path -eq $state.executable -and
                $process.StartTime.ToUniversalTime().Ticks.ToString() -eq $state.start_ticks -and
                $state.data_dir -eq $DataDir) {
                throw '当前工作区服务仍在运行。请先用 stop.cmd 正常停止，再配置目录；资料和任务进度已保留。'
            }
        } catch {
            if ($_.Exception.Message -like '当前工作区服务仍在运行*') { throw }
            throw "无法检查服务状态 $($stateFile.FullName)：$($_.Exception.Message)。请先确认服务停止。"
        }
    }
}
if (-not $ArchiveDir) { $ArchiveDir = Read-Host '归档根目录绝对路径（直接回车保持当前路径）' }
if (-not $ArchiveDir) {
    $current = & $pythonExe -c 'from creator_archive.folders import load_folders; import sys; print(load_folders(sys.argv[1])[0])' $DataDir
    if ($LASTEXITCODE -ne 0) { throw '读取当前目录配置失败，原资料已保留。' }
    $ArchiveDir = $current.Trim()
}
if (-not $ObsidianDir) { $ObsidianDir = Read-Host 'Obsidian目标目录绝对路径（留空则关闭副本）' }
Write-Host '正在检查目录并复制旧归档；旧目录与手工文件不会删除。'
$arguments = @('-X', 'utf8', '-m', 'creator_archive.folders', '--data-dir', $DataDir,
               '--archive-dir', $ArchiveDir)
if ($ObsidianDir) { $arguments += @('--obsidian-dir', $ObsidianDir) }
& $pythonExe @arguments
if ($LASTEXITCODE -ne 0) { throw '目录配置未完成。原配置和旧资料保留；检查上方冲突或空间提示后重试。' }
Write-Host '配置已保存。下次用 start.cmd 启动后，在「数据与接入」检查实际目录。'
