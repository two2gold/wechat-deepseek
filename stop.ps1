# 停止后台服务
# 注意：虚拟环境里的 python.exe 只是转发进程，真正监听端口的是它的子进程，
# 所以这里既结束 PID 树，也按端口兜底清理，避免"停了但端口还占着"。
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

$port = "8000"
if (Test-Path .env) {
    $l = Get-Content .env | Select-String '^PORT=' | Select-Object -First 1
    if ($l) { $port = ($l -replace '^PORT=', '').Trim() }
}

$killed = @()

$pidFile = "logs\server.pid"
if (Test-Path $pidFile) {
    $target = (Get-Content $pidFile | Select-Object -First 1)
    if ($target -and (Get-Process -Id $target -ErrorAction SilentlyContinue)) {
        & taskkill.exe /PID $target /T /F 2>&1 | Out-Null
        $killed += $target
    }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
}

$conns = Get-NetTCPConnection -LocalPort ([int]$port) -State Listen -ErrorAction SilentlyContinue
foreach ($c in $conns) {
    if ($c.OwningProcess -and ($killed -notcontains $c.OwningProcess)) {
        Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
        $killed += $c.OwningProcess
    }
}

if ($killed.Count -gt 0) {
    Write-Host ("已停止服务（PID: " + ($killed -join ", ") + "）") -ForegroundColor Green
} else {
    Write-Host "没有发现正在运行的服务" -ForegroundColor Yellow
}
