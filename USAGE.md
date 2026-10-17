# 日常使用卡（一页纸）

> 忘了怎么用时看这里。完整说明见 [README.md](README.md)。

## 1. 启动 / 停止机器人

| 操作 | 怎么做 |
| --- | --- |
| **启动** | 双击 **`wxbot.cmd`**（拉起机器人 + 公网隧道，窗口里显示状态和后台该填的 URL） |
| **停止** | **关掉那个窗口**（或窗口里按 `Ctrl+C`） |
| 只在本机跑、不开隧道 | `powershell -ExecutionPolicy Bypass -File .\wxbot.ps1 -NoTunnel` |
| 不想看消息视图 | `wxbot.cmd -NoMessages` |

关窗口时机器人和隧道**一起结束**（Windows Job Object 保证，强杀也干净）。

## 2. 看消息 / 人工回复

| 需求 | 命令 |
| --- | --- |
| 实时看用户消息（带时间） | `wxbot.cmd` 窗口里已经自动显示；或单独开：**`messages.cmd`** |
| 只看历史 | `messages.cmd --once --last 50` |
| 以公众号身份手动回一句 | **`reply.cmd`** → `1` 选人 → 输入内容回车 |
| 群发 | `reply.cmd --all` |

看消息是**只读**的，和正在跑的机器人互不干扰。

## 3. 配置在哪

全部在 **`.env`**（这个文件**不会**进 Git）：

```ini
WECHAT_APPID / WECHAT_APPSECRET   # 测试号凭据
WECHAT_TOKEN                       # 后台「服务器配置」里的 Token
DEEPSEEK_API_KEY                   # DeepSeek 密钥
TUNNEL_PROVIDER=serveo             # 隧道服务商
TUNNEL_SUBDOMAIN=<你的子域名>       # 固定公网地址
REPLY_MODE=hybrid                  # 回复策略
```

改完 `.env` 要**重启窗口**才生效。

## 4. 排查三连

```powershell
# ① 本机服务活着吗
curl http://127.0.0.1:8000/healthz

# ② 公网地址活着吗（把域名换成你自己的）
curl https://<你的子域名>.serveousercontent.com/healthz

# ③ 凭据/通道体检（AppID、客服消息、DeepSeek 全查一遍）
.\.venv\Scripts\python.exe tools\check_credentials.py
```

日志：`logs\server.err.log`（应用）、`logs\messages.log`（消息记录）、`logs\tunnel.err.log`（隧道）。

## 5. 改完代码 → 上线（不碰网页）

```powershell
# ① 体检：确认没有把密钥写进代码
.\.venv\Scripts\python.exe tools\check_publish.py

# ② 本地重启验证（关掉 wxbot 窗口再双击）
#    发一条微信消息，确认能回复

# ③ 提交并推送（SSH 走 ssh.github.com:443，实测 4 秒）
git add -A
git commit -m "说明这次改了什么"
git push
```

## 6. 换机器 / 重装怎么办

```powershell
git clone git@ssh.github.com:two2gold/wechat-deepseek.git
cd wechat-deepseek
Copy-Item .env.example .env        # 然后填回你自己的密钥
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\wxbot.cmd
```

⚠️ `.env` 不在仓库里，**密钥要自己另存一份**（密码管理器 / 加密笔记），否则重装后得重新申请。

## 7. 三条容易忘的铁律

1. **同时只开一个 `wxbot.cmd` 窗口** —— 多开会互相抢同一个子域名，报 `remote port forwarding failed`
2. **关掉后别立刻重开** —— 等服务端释放子域名（窗口会自动每 15 秒重试），或换个没用过的名字
3. **push 之前先跑体检** —— `check_publish.py` 退出码 0 才推
