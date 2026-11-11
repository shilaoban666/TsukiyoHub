# 一键自检：编译 + 单测 +（可选）启动服务跑冒烟
#   .\scripts\check.ps1              # 编译 + 测试
#   .\scripts\check.ps1 -Smoke       # 额外启动服务并跑冒烟脚本
param(
    [switch]$Smoke,
    [int]$Port = 8099,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $Python) {
    $venv = Join-Path $root ".venv\Scripts\python.exe"
    if (Test-Path $venv) { $Python = $venv } else { $Python = "python" }
}

$env:PYTHONPATH = Join-Path $root "server"
$env:TSUKIYO_DATA_ROOT = Join-Path $root "data"

Write-Host "== 编译检查 ==" -ForegroundColor Cyan
& $Python -m compileall -q (Join-Path $root "server\tsukiyohub")

Write-Host "== 单元测试 ==" -ForegroundColor Cyan
& $Python -m pytest (Join-Path $root "tests") -q
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if ($Smoke) {
    Write-Host "== 启动服务并冒烟 ==" -ForegroundColor Cyan
    $proc = Start-Process -FilePath $Python -ArgumentList "-m", "tsukiyohub", "--host", "127.0.0.1", "--port", $Port `
        -WorkingDirectory $root -PassThru -WindowStyle Hidden
    try {
        $ready = $false
        for ($i = 0; $i -lt 60; $i++) {
            Start-Sleep -Milliseconds 500
            try {
                Invoke-RestMethod "http://127.0.0.1:$Port/healthz" | Out-Null
                $ready = $true; break
            } catch { }
        }
        if (-not $ready) { throw "服务 30 秒内没有就绪（端口 $Port）" }
        & $Python (Join-Path $root "scripts\smoke.py") --base "http://127.0.0.1:$Port"
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    } finally {
        if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
    }
}

Write-Host "全部完成 ✓" -ForegroundColor Green