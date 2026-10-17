# 一把梭配置：填入公众号与 DeepSeek 凭据，自动改写 .env、重启服务并自检
#
# 用法：
#   .\configure.cmd -AppId wxYOUR_APPID -AppSecret 你的appsecret -DeepSeekKey sk-xxxx
# 只配 DeepSeek（暂时没公众号凭据）：
#   .\configure.cmd -DeepSeekKey sk-xxxx
param(
    [string]$AppId = "",
    [string]$AppSecret = "",
    [string]$DeepSeekKey = "",
    [string]$Model = ""
)

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "已从 .env.example 创建 .env" -ForegroundColor Yellow
}

function Set-EnvValue([string]$Key, [string]$Value) {
    $lines = Get-Content .env -Encoding UTF8
    $found = $false
    $out = foreach ($l in $lines) {
        if ($l -match "^$Key=") { $found = $true; "$Key=$Value" } else { $l }
    }
    if (-not $found) { $out += "$Key=$Value" }
    $out | Set-Content .env -Encoding UTF8
}

if ($AppId) { Set-EnvValue "WECHAT_APPID" $AppId; Write-Host "WECHAT_APPID 已写入" -ForegroundColor Green }
if ($AppSecret) { Set-EnvValue "WECHAT_APPSECRET" $AppSecret; Write-Host "WECHAT_APPSECRET 已写入" -ForegroundColor Green }
if ($DeepSeekKey) {
    Set-EnvValue "DEEPSEEK_API_KEY" $DeepSeekKey
    Set-EnvValue "DEEPSEEK_MOCK" "0"
    Write-Host "DEEPSEEK_API_KEY 已写入，并关闭模拟模式" -ForegroundColor Green
}
if ($Model) { Set-EnvValue "DEEPSEEK_MODEL" $Model; Write-Host "DEEPSEEK_MODEL=$Model" -ForegroundColor Green }

Write-Host "`n重启服务……"
& powershell -NoProfile -ExecutionPolicy Bypass -File ".\stop.ps1"
Start-Sleep -Seconds 2
& powershell -NoProfile -ExecutionPolicy Bypass -File ".\start-background.ps1"
Start-Sleep -Seconds 3

Write-Host "`n---- 健康检查 ----"
try { (Invoke-WebRequest -Uri "http://127.0.0.1:8000/healthz" -UseBasicParsing -TimeoutSec 10).Content } catch { Write-Host "失败: $($_.Exception.Message)" -ForegroundColor Red }

Write-Host "`n---- 配置提醒（看 logs\server.err.log）----"
Get-Content "logs\server.err.log" -Encoding UTF8 -ErrorAction SilentlyContinue | Select-String "配置提醒|服务已启动" | Select-Object -Last 5

Write-Host "`n---- 本地发一条消息自检 ----"
$token = ((Get-Content .env | Select-String '^WECHAT_TOKEN=') -replace '^WECHAT_TOKEN=', '').Trim()
& .\.venv\Scripts\python.exe tools\fake_wechat.py --url http://127.0.0.1:8000/wechat --token $token --text "配置完成后的自检消息" 2>&1 | Select-Object -First 10
