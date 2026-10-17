# 关闭公网隧道
# 注意：start-tunnel.cmd 默认带 -Watch 自愈循环，只杀 ssh 进程的话它 30 秒后会自己重连。
# 所以这里先放一个停止标记（logs\tunnel.stop），自愈循环看到就会退出，然后再结束 ssh。
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
New-Item -ItemType Directory -Force -Path logs | Out-Null

New-Item -ItemType File -Force -Path "logs\tunnel.stop" | Out-Null

$pidFile = "logs\tunnel.pid"
if (-not (Test-Path $pidFile)) {
    Write-Host "没有找到隧道 PID 文件（隧道可能没在运行）" -ForegroundColor Yellow
    exit 0
}

$target = Get-Content $pidFile | Select-Object -First 1
if ($target -and (Get-Process -Id $target -ErrorAction SilentlyContinue)) {
    Stop-Process -Id $target -Force
    Write-Host "隧道已停止（PID=$target）" -ForegroundColor Green
} else {
    Write-Host "PID 文件存在但进程已不在" -ForegroundColor Yellow
}
Remove-Item $pidFile -ErrorAction SilentlyContinue
Write-Host "自愈循环会在下一次探测时退出（最多 30 秒）"
