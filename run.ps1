# Windows 一键启动（首次会创建虚拟环境并装依赖）
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path .env)) {
    Write-Host "缺少 .env：先执行 Copy-Item .env.example .env，再填写 WECHAT_TOKEN / DEEPSEEK_API_KEY" -ForegroundColor Yellow
    exit 1
}

if (-not (Test-Path .venv)) {
    python -m venv .venv
}
.\.venv\Scripts\python.exe -m pip install -q --upgrade pip
.\.venv\Scripts\python.exe -m pip install -q -r requirements.txt
.\.venv\Scripts\python.exe run.py
