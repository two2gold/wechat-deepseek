# 后台常驻启动：关掉终端也不会停，日志写到 logs\
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path .env)) {
    Write-Host "缺少 .env：先执行 Copy-Item .env.example .env 并填写配置" -ForegroundColor Yellow
    exit 1
}

$python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "首次运行：创建虚拟环境并安装依赖……"
    python -m venv .venv
    & .\.venv\Scripts\python.exe -m pip install -q -r requirements.txt
}

New-Item -ItemType Directory -Force -Path logs | Out-Null
$pidFile = "logs\server.pid"
if (Test-Path $pidFile) {
    $old = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($old -and (Get-Process -Id $old -ErrorAction SilentlyContinue)) {
        Write-Host "服务已在运行，PID=$old（要重启先执行 .\stop.ps1）" -ForegroundColor Yellow
        exit 0
    }
}

$proc = Start-Process -FilePath $python -ArgumentList "run.py" -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput "logs\server.out.log" -RedirectStandardError "logs\server.err.log"
$proc.Id | Set-Content $pidFile

Start-Sleep -Seconds 3
Write-Host "已启动，PID=$($proc.Id)" -ForegroundColor Green
Write-Host "健康检查： http://127.0.0.1:8000/healthz"
Write-Host "运行日志： Get-Content logs\server.err.log -Encoding UTF8 -Wait"
Write-Host "停止服务： .\stop.ps1"
