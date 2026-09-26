$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    throw '请先执行：python -m venv .venv，然后 .venv/Scripts/python.exe -m pip install -r requirements.lock'
}
Write-Host 'Creator Archive 验证模式：http://127.0.0.1:8765（Ctrl+C 停止）'
& .venv/Scripts/python.exe -m creator_archive
