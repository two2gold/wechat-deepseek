# ============================================================================
# 公众号助手 · 本地开关
#
#   双击 wxbot.cmd 启动：拉起机器人 + 公网隧道，窗口里实时显示状态。
#   关掉窗口（或按 Ctrl+C）：机器人与隧道一起停止。
#
# 说明：
#   - 微信服务器是主动回调的，所以"地址变了"必须回后台改一次；
#     脚本在地址变化时会尝试用客服消息把新地址推到你微信（需要 .env 里的 NOTIFY_OPENIDS）。
#   - 如果用固定域名（Cloudflare Tunnel / VPS），后台只需填一次，之后开关机都不影响。
#   - 若窗口被强制结束导致进程残留，下次启动会自动清理；也可手动跑 stop.cmd / stop-tunnel.cmd。
# ============================================================================
param(
    [int]$RefreshSeconds = 5,
    [switch]$NoTunnel,
    [switch]$NoMessages,
    [string]$Provider = ""
)

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
New-Item -ItemType Directory -Force -Path logs | Out-Null

# ---------------------------------------------------------------------------
# Job Object：让子进程"父死子亡"。
# Windows 上父进程被强杀（关窗口、任务管理器结束、崩溃）时子进程不会自动结束，
# 会残留 python 和 ssh。把子进程加入 KILL_ON_JOB_CLOSE 的作业对象后，
# 只要本脚本进程消失（无论怎么消失），内核就会连子进程一起清掉。
# ---------------------------------------------------------------------------
$script:jobReady = $false
try {
    if (-not ("KillerJob" -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class KillerJob
{
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr CreateJobObject(IntPtr attrs, string name);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(IntPtr job, int infoClass, IntPtr info, uint length);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [StructLayout(LayoutKind.Sequential)]
    private struct BASIC_LIMIT
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IO_COUNTERS
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct EXTENDED_LIMIT
    {
        public BASIC_LIMIT BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    private const int ExtendedLimitInformation = 9;
    private const uint KILL_ON_JOB_CLOSE = 0x2000;
    private static IntPtr _job = IntPtr.Zero;

    public static bool Init()
    {
        if (_job != IntPtr.Zero) return true;
        _job = CreateJobObject(IntPtr.Zero, null);
        if (_job == IntPtr.Zero) return false;
        var info = new EXTENDED_LIMIT();
        info.BasicLimitInformation.LimitFlags = KILL_ON_JOB_CLOSE;
        int size = Marshal.SizeOf(info);
        IntPtr ptr = Marshal.AllocHGlobal(size);
        try
        {
            Marshal.StructureToPtr(info, ptr, false);
            return SetInformationJobObject(_job, ExtendedLimitInformation, ptr, (uint)size);
        }
        finally { Marshal.FreeHGlobal(ptr); }
    }

    public static bool Add(IntPtr processHandle)
    {
        if (_job == IntPtr.Zero) return false;
        return AssignProcessToJobObject(_job, processHandle);
    }
}
'@
    }
    $script:jobReady = [KillerJob]::Init()
} catch {
    Write-Host "Note: Job Object init failed ($($_.Exception.Message)); falling back to cleanup-on-exit plus cleanup-on-next-start" -ForegroundColor Yellow
}

function Add-ToJob($Proc) {
    if (-not $script:jobReady -or -not $Proc) { return }
    try { [void][KillerJob]::Add($Proc.Handle) } catch { }
}

function Get-EnvValue([string]$Key, [string]$Default = "") {
    if (-not (Test-Path .env)) { return $Default }
    $line = Get-Content .env -Encoding UTF8 | Select-String "^$Key=" | Select-Object -First 1
    if (-not $line) { return $Default }
    $value = ($line -replace "^$Key=", "").Trim()
    if ($value) { return $value }
    return $Default
}

$port = Get-EnvValue "PORT" "8000"
$token = Get-EnvValue "WECHAT_TOKEN" ""
$subdomain = Get-EnvValue "TUNNEL_SUBDOMAIN" ""
# 注意：PowerShell 变量名大小写不敏感，参数 $Provider 与 $provider 是同一个变量，
# 所以解析结果必须换一个名字，否则会把命令行传入的值覆盖掉（踩过一次）。
$providerName = (Get-EnvValue "TUNNEL_PROVIDER" "localhost.run").ToLower()
if ($Provider) { $providerName = $Provider.ToLower() }
$notifyOpenids = Get-EnvValue "NOTIFY_OPENIDS" ""
$python = ".\.venv\Scripts\python.exe"
$sshExe = "$env:SystemRoot\System32\OpenSSH\ssh.exe"
$keyPath = Join-Path $env:USERPROFILE ".ssh\dsh_localhost_run"

$script:serverProc = $null
$script:tunnelProc = $null
$script:msgProc = $null
$script:publicUrl = ""

function Stop-Everything {
    if ($script:msgProc) { Stop-Process -Id $script:msgProc.Id -Force -ErrorAction SilentlyContinue }
    if ($script:tunnelProc) { Stop-Process -Id $script:tunnelProc.Id -Force -ErrorAction SilentlyContinue }
    if ($script:serverProc) {
        & taskkill.exe /PID $script:serverProc.Id /T /F 2>&1 | Out-Null
    }
    # 端口兜底：虚拟环境的 python 是转发进程，真正监听的可能是子进程
    Get-NetTCPConnection -LocalPort ([int]$port) -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    Remove-Item "logs\server.pid", "logs\tunnel.pid" -ErrorAction SilentlyContinue
}

function Start-MessageView {
    # 把"用户消息 + 时间"实时打在同一个窗口里（子进程直接写本控制台）。
    # 也同步追加到 logs\messages.log，终端显示异常时可以打开那个文件看。
    if (-not (Test-Path "tools\tail_messages.py")) { return }
    $script:msgProc = Start-Process -FilePath $python `
        -ArgumentList @("tools\tail_messages.py", "--last", "8") `
        -PassThru -NoNewWindow
    Add-ToJob $script:msgProc
}

function Test-Local {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/healthz" -UseBasicParsing -TimeoutSec 5
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Test-Public([string]$Url) {
    if (-not $Url) { return $false }
    $code = [string](& curl.exe -sS --noproxy "*" -m 12 -o NUL -w "%{http_code}" "$Url/healthz" 2>$null)
    return ($code.Trim() -eq "200")
}

function Start-Tunnel {
    if (-not (Test-Path $sshExe)) { Write-Host "ssh.exe not found - skipping the tunnel" -ForegroundColor Red; return "" }
    Remove-Item "logs\tunnel.out.log", "logs\tunnel.err.log" -ErrorAction SilentlyContinue

    $sshArgs = @(
        '-o', 'StrictHostKeyChecking=accept-new',
        '-o', ("UserKnownHostsFile=" + $env:TEMP + "\kh_$($providerName -replace '[^a-z0-9]','')"),
        '-o', 'ServerAliveInterval=30',
        '-o', 'ExitOnForwardFailure=yes'
    )
    if (Test-Path $keyPath) { $sshArgs += @('-i', $keyPath, '-o', 'IdentitiesOnly=yes') }

    if ($providerName -eq 'serveo') {
        # serveo 的写法：子域名写在 -R 参数里；不写就是随机地址。
        # 免费版只对浏览器插"Warning"页，机器对机器的 webhook 流量直通（已实测）。
        $forward = if ($subdomain) { "${subdomain}:80:127.0.0.1:$port" } else { "80:127.0.0.1:$port" }
        $sshArgs += @('-R', $forward, 'serveo.net')
    } else {
        # localhost.run 的写法：子域名当 SSH 用户名（需先注册公钥）
        $remote = if ($subdomain) { "$subdomain@localhost.run" } else { 'nokey@localhost.run' }
        $sshArgs += @('-R', "80:127.0.0.1:$port", $remote)
    }

    $script:tunnelProc = Start-Process -FilePath $sshExe -PassThru -NoNewWindow -ArgumentList $sshArgs `
        -RedirectStandardOutput "logs\tunnel.out.log" -RedirectStandardError "logs\tunnel.err.log"
    Add-ToJob $script:tunnelProc

    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 2
        $out = ""
        if (Test-Path "logs\tunnel.out.log") {
            $out = [string](Get-Content "logs\tunnel.out.log" -Raw -ErrorAction SilentlyContinue)
        }
        if ($out) {
            $url = ([regex]::Match($out, 'https://[a-zA-Z0-9\.\-]+\.(lhr\.life|localhost\.run|serveo\.net|serveousercontent\.com)')).Value
            if ($url) {
                $script:tunnelProc.Id | Set-Content "logs\tunnel.pid"
                $url | Set-Content "logs\tunnel.url"
                return $url
            }
        }
    }

    # 失败时给出针对性解释：最常见的就是"子域名被占用"
    $errText = ""
    if (Test-Path "logs\tunnel.err.log") {
        $errText = [string](Get-Content "logs\tunnel.err.log" -Raw -ErrorAction SilentlyContinue)
    }
    if ($errText -match 'forwarding failed|in use|already|taken') {
        Write-Host ""
        Write-Host "! Subdomain '$subdomain' is not available (remote port forwarding failed)." -ForegroundColor Yellow
        Write-Host "  Most likely another window of yours (e.g. serveo-register.cmd) still holds that name." -ForegroundColor Yellow
        Write-Host "  Fix: close that window and start this program again, or change TUNNEL_SUBDOMAIN in .env." -ForegroundColor Yellow
    } elseif ($providerName -eq 'serveo' -and $subdomain) {
        Write-Host ""
        Write-Host "! serveo returned no address: make sure this SSH key is registered (serveo-register.cmd prints the link)." -ForegroundColor Yellow
    }
    return ""
}

function Notify-NewUrl([string]$Url) {
    if (-not $notifyOpenids) { return }
    $text = "⚠️ 机器人公网地址已变更，请到公众号后台把服务器 URL 改成：`n$Url/wechat"
    & $python tools\notify.py --text $text 2>&1 | Out-Null
}

function Set-ConsoleLayout {
    # 控制台"缓冲区比窗口宽"时会出现横向滚动：画面左侧被截掉、行看着错位
    # （症状就是文字被从中间切开，左边少一块）。这里强制让
    # 缓冲区宽度 == 窗口宽度 == 100，并把视图滚回左上角。
    # 终端不支持这些 API 时会抛异常，忽略即可，不影响功能。
    try {
        $raw = $Host.UI.RawUI
        $w = 100
        $win = $raw.WindowSize
        if ($win.Width -gt $w) { $win.Width = $w; $raw.WindowSize = $win }
        $buf = $raw.BufferSize
        if ($buf.Width -ne $w) { $buf.Width = $w; $raw.BufferSize = $buf }
        $win = $raw.WindowSize
        $win.Width = $w
        if ($win.Height -lt 28) { $win.Height = 28 }
        $raw.WindowSize = $win
        $raw.WindowPosition = New-Object System.Management.Automation.Host.Coordinates 0, 0
    } catch { }
}

function Write-Event([string]$Text, [string]$Color = "Gray") {
    # 只追加、不重画。之前用 Clear-Host 每 5 秒重画一次，终端会反复 reflow，
    # 内容容易被搞乱；追加模式下终端怎么滚都不会破坏已有内容。
    Write-Host ("[" + (Get-Date).ToString('HH:mm:ss') + "] " + $Text) -ForegroundColor $Color
}

function Show-Header {
    $rule = "=" * 58
    Write-Host ""
    Write-Host $rule -ForegroundColor Cyan
    Write-Host " WeChat x DeepSeek bot  (close window to stop)" -ForegroundColor Cyan
    Write-Host $rule -ForegroundColor Cyan
    Write-Host ""
}

function Show-UrlBlock {
    Write-Host ""
    Write-Host "----------------------------------------------------------" -ForegroundColor Green
    Write-Host " WeChat backend URL:" -ForegroundColor Green
    Write-Host "   $($script:publicUrl)/wechat" -ForegroundColor Green
    Write-Host " Token:" -ForegroundColor Green
    Write-Host "   $token" -ForegroundColor Green
    Write-Host "----------------------------------------------------------" -ForegroundColor Green
    Write-Host ""
}

# ---------------------------------------------------------------- 启动
Set-ConsoleLayout
Show-Header
Write-Event "cleaning up leftovers from a previous run ..."
& powershell -NoProfile -ExecutionPolicy Bypass -File ".\stop.ps1" 2>&1 | Out-Null
if (Test-Path "logs\tunnel.pid") {
    $old = Get-Content "logs\tunnel.pid" | Select-Object -First 1
    if ($old) { Stop-Process -Id $old -Force -ErrorAction SilentlyContinue }
}
Remove-Item "logs\tunnel.stop" -ErrorAction SilentlyContinue

if (-not (Test-Path $python)) {
    Write-Event "first run: creating venv and installing dependencies ..." "Yellow"
    python -m venv .venv
    & .\.venv\Scripts\python.exe -m pip install -q -r requirements.txt
}

Write-Event "starting the bot ..."
$script:serverProc = Start-Process -FilePath $python -ArgumentList "run.py" -PassThru -NoNewWindow `
    -RedirectStandardOutput "logs\server.out.log" -RedirectStandardError "logs\server.err.log"
Add-ToJob $script:serverProc
$script:serverProc.Id | Set-Content "logs\server.pid"
Start-Sleep -Seconds 4

if (-not (Test-Local)) {
    Write-Event "bot failed to start - tail of logs\server.err.log:" "Red"
    Get-Content "logs\server.err.log" -Encoding UTF8 -ErrorAction SilentlyContinue | Select-Object -Last 8
    Stop-Everything
    exit 1
}
Write-Event "service is up:  http://127.0.0.1:$port" "Green"
Write-Event "provider: $providerName$(if ($subdomain) { " / $subdomain" })"

if (-not $NoMessages) {
    Write-Event "message view: on (new user messages show up below with timestamps)" "DarkGray"
    Write-Event "              also appended to logs\messages.log" "DarkGray"
    Start-MessageView
}

if (-not $NoTunnel) {
    Write-Event "opening the public tunnel (up to 40s) ..."
    $script:publicUrl = Start-Tunnel
    if ($script:publicUrl) {
        Write-Event "tunnel is up" "Green"
        Show-UrlBlock
    } else {
        Write-Event "tunnel not up yet - will keep retrying every 15s" "Yellow"
    }
}
Write-Event "close this window (or Ctrl+C) to stop" "DarkGray"

# ---------------------------------------------------------------- 主循环
$failures = 0
$maxFailures = 3
$serviceWasUp = $true
try {
    while ($true) {
        Start-Sleep -Seconds $RefreshSeconds

        if (-not (Test-Local)) {
            if ($serviceWasUp) {
                Write-Event "service is NOT responding - check logs\server.err.log" "Red"
                $serviceWasUp = $false
            }
            continue
        } elseif (-not $serviceWasUp) {
            Write-Event "service is back up" "Green"
            $serviceWasUp = $true
        }

        if ($NoTunnel) { continue }

        # 隧道还没建立时（常见于子域名刚被上一个窗口占用、服务端还没释放），
        # 每 15 秒重试一次，抢到就把地址打出来。
        if (-not $script:publicUrl) {
            $script:publicUrl = Start-Tunnel
            if ($script:publicUrl) {
                Write-Event "tunnel is up" "Green"
                Show-UrlBlock
            } else {
                $failures++
                if ($failures % 4 -eq 1) {
                    Write-Event "tunnel still not up - retrying" "Yellow"
                }
            }
            Start-Sleep -Seconds 15
            continue
        }

        if (Test-Public $script:publicUrl) {
            $failures = 0
        } else {
            $failures++
            if ($failures -ge $maxFailures) {
                $failures = 0
                Write-Event "tunnel looks dead - reconnecting ..." "Yellow"
                $oldUrl = $script:publicUrl
                $script:publicUrl = Start-Tunnel
                if ($script:publicUrl) {
                    if ($script:publicUrl -ne $oldUrl) {
                        Write-Event "tunnel restored with a NEW address" "Yellow"
                        Notify-NewUrl $script:publicUrl
                        Show-UrlBlock
                    } else {
                        Write-Event "tunnel restored" "Green"
                    }
                } else {
                    Write-Event "reconnect failed - will retry every 15s" "Red"
                    $script:publicUrl = ""
                }
            }
        }
    }
} finally {
    Write-Host ""
    Write-Event "stopping the bot and the tunnel ..." "Yellow"
    Stop-Everything
    Write-Event "all stopped." "Green"
}
