# 开发环境启动脚本：不装依赖、直接用项目内的 Python 跑起来
#   .\scripts\dev.ps1            # 启动后端（热重载）
#   .\scripts\dev.ps1 -Web       # 同时启动前端
param(
    [switch]$Web,
    [int]$Port = 8080,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

if (-not $Python) {
    $venv = Join-Path $root ".venv\Scripts\python.exe"
    if (Test-Path $venv) { $Python = $venv } else { $Python = "python" }
}

$env:PYTHONPATH = Join-Path $root "server"
if (-not (Test-Path (Join-Path $root ".env"))) {
    Copy-Item (Join-Path $root ".env.example") (Join-Path $root ".env")
    Write-Host "已从 .env.example 生成 .env（未填密钥时使用演示模型）" -ForegroundColor Yellow
}

if ($Web) {
    $webDir = Join-Path $root "web"
    if (Test-Path $webDir) {
        Start-Process -FilePath "npm" -ArgumentList "run", "dev" -WorkingDirectory $webDir
        Write-Host "前端已在后台启动（默认 http://127.0.0.1:5173）" -ForegroundColor Cyan
    }
}

Write-Host "启动 TsukiyoHub：http://127.0.0.1:$Port （文档 /docs）" -ForegroundColor Cyan
& $Python -m tsukiyohub --host 127.0.0.1 --port $Port --reload