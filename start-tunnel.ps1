# 公网隧道（SSH 反向隧道，出口 443 HTTPS，符合微信 80/443 端口要求）
#
# 用法：
#   .\start-tunnel.ps1                 # 建立一次隧道，打印公网地址后退出
#   .\start-tunnel.ps1 -Watch          # 自我修复模式：隧道被回收就自动重连（推荐）
#   .\start-tunnel.ps1 -Key C:\path\to\key
#
# 三个关键点：
# 1) 必须用 127.0.0.1，不能写 localhost —— Windows 上 localhost 先解析成 IPv6 ::1，
#    而 uvicorn 默认只监听 IPv4，症状是公网请求 "Empty reply from server"。
# 2) 在 https://admin.localhost.run/ 上传本机公钥后，子域会固定下来，不再每次随机。
#    脚本默认使用 %USERPROFILE%\.ssh\dsh_localhost_run 这把私钥。
# 3) -Watch 会每 30 秒探测一次公网地址（而不是只看进程在不在）——
#    实测会出现"ssh 进程还活着但对端已回收隧道"的情况，只看进程会漏判。
#    但连续探测失败要够 MaxFailures 次才重连：否则你自己重启服务的那几秒
#    会被误判成"隧道死了"，白白换掉一个还能用的公网地址。
param(
    [string]$Key = "",
    [string]$Subdomain = "",
    [switch]$Watch,
    [int]$IntervalSeconds = 30,
    [int]$MaxFailures = 3
)

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
New-Item -ItemType Directory -Force -Path logs | Out-Null

if (-not $Key) { $Key = Join-Path $env:USERPROFILE ".ssh\dsh_localhost_run" }
# 清掉上一次 stop-tunnel 留下的停止标记，否则刚起来就会被自愈循环判定为"该退出"
Remove-Item "logs\tunnel.stop" -ErrorAction SilentlyContinue
$ssh = "$env:SystemRoot\System32\OpenSSH\ssh.exe"
if (-not (Test-Path $ssh)) { Write-Host "找不到 ssh.exe，请先安装 OpenSSH 客户端" -ForegroundColor Red; exit 1 }

$port = "8000"
$token = ""
$envSubdomain = ""
if (Test-Path .env) {
    $line = Get-Content .env | Select-String '^PORT=' | Select-Object -First 1
    if ($line) { $port = ($line -replace '^PORT=', '').Trim() }
    $token = ((Get-Content .env | Select-String '^WECHAT_TOKEN=') -replace '^WECHAT_TOKEN=', '').Trim()
    $sub = Get-Content .env | Select-String '^TUNNEL_SUBDOMAIN=' | Select-Object -First 1
    if ($sub) { $envSubdomain = ($sub -replace '^TUNNEL_SUBDOMAIN=', '').Trim() }
}
# 优先级：命令行参数 > .env 里的 TUNNEL_SUBDOMAIN
if (-not $Subdomain) { $Subdomain = $envSubdomain }

function Stop-TunnelNow {
    $pidFile = "logs\tunnel.pid"
    if (-not (Test-Path $pidFile)) { return }
    $old = Get-Content $pidFile | Select-Object -First 1
    if ($old -and (Get-Process -Id $old -ErrorAction SilentlyContinue)) {
        Stop-Process -Id $old -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
}

function Start-TunnelOnce {
    Stop-TunnelNow
    Remove-Item "logs\tunnel.out.log", "logs\tunnel.err.log" -ErrorAction SilentlyContinue

    $sshArgs = @(
        '-o', 'StrictHostKeyChecking=no',
        '-o', ("UserKnownHostsFile=" + $env:TEMP + "\kh_lnr"),
        '-o', 'ServerAliveInterval=30',
        '-o', 'ExitOnForwardFailure=yes'
    )
    if (Test-Path $Key) { $sshArgs += @('-i', $Key, '-o', 'IdentitiesOnly=yes') }
    # localhost.run 的规则：想要的子域名就是 SSH 用户名（例如 wxai@localhost.run）。
    # 但它要求该公钥已在 https://admin.localhost.run/ 注册过，否则会被拒绝（permission denied）。
    $remote = if ($Subdomain) { "$Subdomain@localhost.run" } else { 'nokey@localhost.run' }
    $sshArgs += @('-R', "80:127.0.0.1:$port", $remote)

    $proc = Start-Process -FilePath $ssh -PassThru -WindowStyle Hidden -ArgumentList $sshArgs `
        -RedirectStandardOutput "logs\tunnel.out.log" -RedirectStandardError "logs\tunnel.err.log"

    $url = ""
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 2
        # 日志文件此刻可能还不存在，$out 会是 $null，直接丢给 [regex]::Match 会抛异常中断脚本
        $out = ""
        if (Test-Path "logs\tunnel.out.log") {
            $out = [string](Get-Content "logs\tunnel.out.log" -Raw -ErrorAction SilentlyContinue)
        }
        if ($out) {
            $url = ([regex]::Match($out, 'https://[a-zA-Z0-9\.\-]+\.(lhr\.life|localhost\.run)')).Value
        }
        if ($url) { break }
    }

    if (-not $url) {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        return ""
    }
    $proc.Id | Set-Content "logs\tunnel.pid"
    $url | Set-Content "logs\tunnel.url"
    Write-Host "隧道已建立（ssh PID=$($proc.Id)）"
    return $url
}

function Test-Public {
    param([string]$Url)
    if (-not $Url) { return $false }
    $code = [string](& curl.exe -sS --noproxy "*" -m 15 -o NUL -w "%{http_code}" "$Url/healthz" 2>$null)
    return ($code.Trim() -eq "200")
}

if (-not (Test-Path $Key)) {
    Write-Host "提示：未找到 SSH 私钥 $Key" -ForegroundColor Yellow
    Write-Host "      当前是匿名隧道：地址随机、几十分钟就可能被对端回收。" -ForegroundColor Yellow
    Write-Host "      注册后子域可固定，做法见 README 4.3。" -ForegroundColor Yellow
    Write-Host ""
}

$url = Start-TunnelOnce
if (-not $url) {
    Write-Host "隧道未能建立，请看 logs\tunnel.err.log" -ForegroundColor Red
    if ($Subdomain) {
        Write-Host "提示：你指定了固定子域名 '$Subdomain'，它要求公钥已注册 —— 请先到 https://admin.localhost.run/ 上传公钥，并确认该子域名没有被别人占用。" -ForegroundColor Yellow
        Write-Host "      临时先用随机地址：重新运行 .\start-tunnel.ps1 -Subdomain ''" -ForegroundColor Yellow
    }
    exit 1
}

Write-Host ""
Write-Host "公网地址： $url" -ForegroundColor Green
Write-Host "后台填写： $url/wechat" -ForegroundColor Green
if ($token) { Write-Host "Token  ： $token" -ForegroundColor Green }
Write-Host ""
Write-Host "自检： .\.venv\Scripts\python.exe tools\fake_wechat.py --url $url/wechat --token $token --check-only"
Write-Host "停止： .\stop-tunnel.cmd"
Write-Host ""

if (-not $Watch) { exit 0 }

Write-Host "已进入自我修复模式：每 $IntervalSeconds 秒探测一次，连续 $MaxFailures 次失败才重连（按 Ctrl+C 或运行 .\stop-tunnel.cmd 退出）" -ForegroundColor Cyan
$failures = 0
while ($true) {
    Start-Sleep -Seconds $IntervalSeconds
    # stop-tunnel.cmd 会留下这个标记，自愈循环看到就干净退出，否则它会一直重连
    if (Test-Path "logs\tunnel.stop") {
        Remove-Item "logs\tunnel.stop" -ErrorAction SilentlyContinue
        Write-Host "收到停止标记，自愈循环退出" -ForegroundColor Yellow
        exit 0
    }
    if (Test-Public $url) {
        if ($failures -gt 0) { Write-Host "[$((Get-Date).ToString('HH:mm:ss'))] 探测恢复正常" -ForegroundColor Green }
        $failures = 0
        continue
    }
    $failures++
    $stamp = (Get-Date).ToString('HH:mm:ss')
    if ($failures -lt $MaxFailures) {
        # 可能只是你自己在重启服务，先观察，别急着换地址
        Write-Host "[$stamp] 探测失败 $failures/$MaxFailures（若你在重启服务可忽略）" -ForegroundColor DarkYellow
        continue
    }
    Write-Host "[$stamp] 连续 $failures 次探测失败，判定隧道已失效，正在重连……" -ForegroundColor Yellow
    $newUrl = Start-TunnelOnce
    if ($newUrl) {
        $url = $newUrl
        Write-Host "[$stamp] 已重连：$url" -ForegroundColor Green
        Write-Host "[$stamp] 后台 URL 请填：$url/wechat" -ForegroundColor Green
    } else {
        Write-Host "[$stamp] 重连失败，$IntervalSeconds 秒后再试" -ForegroundColor Red
    }
    $failures = 0
}
