# 从你的 Windows 电脑一条命令部署到 VPS
#
# 用法（在本机项目根目录）：
#   .\deploy\upload-and-deploy.ps1 -Server root@1.2.3.4 -Domain wx.example.com -Email me@example.com
#
# 可选参数：
#   -Port 22                  SSH 端口
#   -Key C:\path\id_ed25519   用密钥登录（不填就走密码，会提示你输入）
#
# 它会：打包项目（自动排除 .venv/logs/data）→ scp 上传 → 远程解包 → 跑 deploy/bootstrap.sh
# 注意：包里含 .env（真实 AppSecret 和 API Key），全程走 SSH 加密，请勿把 tar 包随手转发。
param(
    [Parameter(Mandatory = $true)][string]$Server,
    [Parameter(Mandatory = $true)][string]$Domain,
    [string]$Email = "",
    [int]$Port = 22,
    [string]$Key = ""
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $root

foreach ($tool in @("tar", "scp", "ssh")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "找不到 $tool 命令。Windows 10 1809+ 自带 tar/scp/ssh；或安装 OpenSSH 客户端后重试。"
    }
}
if (-not (Test-Path ".env")) { throw "本机没有 .env，先在项目根目录配置好再上传。" }

$archive = "deploy\wechat-deepseek.tar.gz"
if (Test-Path $archive) { Remove-Item $archive -Force }

Write-Host "1/3 打包项目（排除 .venv / logs / data）……" -ForegroundColor Cyan
& tar -czf $archive `
    --exclude=.venv --exclude=logs --exclude=data --exclude=__pycache__ `
    --exclude=.pytest_cache --exclude='*.pyc' `
    app tools tests deploy run.py run.sh requirements.txt requirements-dev.txt `
    .env .env.example README.md Dockerfile docker-compose.yml
if ($LASTEXITCODE -ne 0) { throw "打包失败" }
$sizeMb = [math]::Round((Get-Item $archive).Length / 1MB, 2)
Write-Host "    包大小：$sizeMb MB" -ForegroundColor DarkGray

$sshOpts = @("-o", "StrictHostKeyChecking=accept-new", "-p", "$Port")
$scpOpts = @("-o", "StrictHostKeyChecking=accept-new", "-P", "$Port")
if ($Key) {
    $sshOpts += @("-i", $Key)
    $scpOpts += @("-i", $Key)
}

Write-Host "2/3 上传到 $Server（如果提示密码，就输 VPS 的登录密码）……" -ForegroundColor Cyan
& scp @scpOpts $archive "${Server}:/tmp/wechat-deepseek.tar.gz"
if ($LASTEXITCODE -ne 0) { throw "上传失败：检查 IP / 端口 / 密码是否正确" }

Write-Host "3/3 远程解包并部署（首次大约 1~3 分钟）……" -ForegroundColor Cyan
$remoteCmd = "mkdir -p /opt/wechat-deepseek && tar -xzf /tmp/wechat-deepseek.tar.gz -C /opt/wechat-deepseek && cd /opt/wechat-deepseek && bash deploy/bootstrap.sh '$Domain' '$Email'"
& ssh @sshOpts $Server $remoteCmd
if ($LASTEXITCODE -ne 0) { throw "远程部署失败，看上面的远程输出；也可 ssh 上去执行 journalctl -u wechat-deepseek -n 50" }

Write-Host ""
Write-Host "完成。下一步：把 https://$Domain/wechat 填进公众号后台的服务器配置。" -ForegroundColor Green
