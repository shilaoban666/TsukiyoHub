# 路由网关自检：编译 + vet + 单测（可选：起假上游跑端到端冒烟）
#   .\scripts\gateway_check.ps1
#   .\scripts\gateway_check.ps1 -Smoke
param(
    [switch]$Smoke,
    [int]$Port = 18081,
    [int]$AdminPort = 18082,
    [int]$MockPort = 18080,
    [string]$Go = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$gateway = Join-Path $root "gateway"

# ---- 找 Go：显式参数 > 环境变量 > 本地工具链 > PATH ----
if (-not $Go) {
    if ($env:GO_EXE) { $Go = $env:GO_EXE }
    elseif (Test-Path "E:\Project\HQDingtalkRobotUI\.toolchains\go\bin\go.exe") {
        $Go = "E:\Project\HQDingtalkRobotUI\.toolchains\go\bin\go.exe"
    } else { $Go = "go" }
}

# ---- Go 需要可写缓存目录；沙箱里默认 TEMP 可能不可写 ----
$work = Join-Path $env:TEMP "tsukiyo-go"
try {
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    $probe = Join-Path $work ".probe"
    Set-Content -Path $probe -Value "ok" -Encoding utf8
    Remove-Item $probe -Force
} catch {
    $work = Join-Path $root ".gocache"
    New-Item -ItemType Directory -Force -Path $work | Out-Null
}
$env:GOCACHE = Join-Path $work "cache"
$env:GOMODCACHE = Join-Path $work "mod"
$env:GOTMPDIR = Join-Path $work "tmp"
$env:TEMP = $env:GOTMPDIR
$env:TMP = $env:GOTMPDIR
$env:GOTOOLCHAIN = "local"
New-Item -ItemType Directory -Force -Path $env:GOCACHE, $env:GOMODCACHE, $env:GOTMPDIR | Out-Null

Push-Location $gateway
try {
    Write-Host "== go vet ==" -ForegroundColor Cyan
    & $Go vet ./...
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "== go test ==" -ForegroundColor Cyan
    & $Go test ./... -count=1
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "== go build ==" -ForegroundColor Cyan
    $binDir = Join-Path $gateway "bin"
    New-Item -ItemType Directory -Force -Path $binDir | Out-Null
    & $Go build -o (Join-Path $binDir "tsukiyo-gateway.exe") ./cmd/tsukiyo-gateway
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    if (-not $Smoke) { Write-Host "全部完成 ✓（未跑冒烟，加 -Smoke 可跑端到端）" -ForegroundColor Green; exit 0 }

    # ---- 端到端：假上游 + 网关 + 冒烟脚本 ----
    Write-Host "== 端到端冒烟 ==" -ForegroundColor Cyan
    $runDir = Join-Path $root "data\gateway-smoke"
    New-Item -ItemType Directory -Force -Path $runDir | Out-Null
    $configPath = Join-Path $runDir "config.json"
    Copy-Item (Join-Path $gateway "config.test.json") $configPath -Force

    $python = "python"
    $venv = Join-Path $root ".venv\Scripts\python.exe"
    if (Test-Path $venv) { $python = $venv }

    $mock = Start-Process -FilePath $python -ArgumentList (Join-Path $root "scripts\mock_upstream.py"), "--port", $MockPort `
        -PassThru -WindowStyle Hidden
    Start-Sleep -Seconds 2
    $gw = Start-Process -FilePath (Join-Path $binDir "tsukiyo-gateway.exe") `
        -ArgumentList "-config", $configPath, "-addr", ":$Port", "-admin-addr", ":$AdminPort" `
        -PassThru -WindowStyle Hidden
    try {
        $ready = $false
        for ($i = 0; $i -lt 40; $i++) {
            Start-Sleep -Milliseconds 500
            try { Invoke-RestMethod "http://127.0.0.1:$AdminPort/healthz" | Out-Null; $ready = $true; break } catch { }
        }
        if (-not $ready) { throw "网关 20 秒内没有就绪" }
        & $python (Join-Path $root "scripts\gateway_smoke.py") `
            --base "http://127.0.0.1:$Port" --admin "http://127.0.0.1:$AdminPort" `
            --sink "http://127.0.0.1:$MockPort/sink"
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    } finally {
        foreach ($proc in @($gw, $mock)) {
            if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
        }
    }
    Write-Host "全部完成 ✓" -ForegroundColor Green
} finally {
    Pop-Location
}