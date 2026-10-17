# 微信公众号接入 DeepSeek（开箱可用）

把公众号变成你自己的 AI 助手：用户在公众号里发一句话，服务端调用 DeepSeek 生成回答并回给用户。
纯 Python（FastAPI），零数据库依赖（SQLite），支持**明文 / 兼容 / 安全（AES）三种加解密模式**，
并且专门解决了微信「**5 秒必须响应**」这个最容易踩的坑。

---

## 0. 支持范围（先看这段，避免误期待）

**"协议已实现" 和 "真实环境验证过" 是两件事，本项目如实区分：**

| 平台 / 账号类型 | 状态 | 说明 |
| --- | --- | --- |
| **微信公众号 · 测试号（sandbox）** | ✅ **端到端实测** | 用真实 AppID/AppSecret 验证过：微信服务器 URL 校验、真实用户消息、AI 回复、客服消息推送全部跑通 |
| **微信公众号 · 认证服务号 / 认证订阅号** | ⚠️ **协议同构，未实测** | 代码走的是同一套协议，预期可用，但没有正式号凭据所以未验证。注意差异：①IP 白名单（否则 `40164`）②安全模式 AES（测试号后台没有 EncodingAESKey，所以 **AES 路径只被模拟请求验证过**）③客服消息权限 |
| **微信公众号 · 未认证个人订阅号** | ❌ **无法接入** | 后台没有「服务器配置」入口，这是平台限制，不是代码问题 |
| **企业微信（WeCom，"企业号"）** | ⚠️ 未实现（易扩展） | 回调验证机制与公众号几乎一致（`msg_signature` + AES + `echostr`），差异在 XML 字段、`corpid/corpsecret` 换 token、发消息要 `agentid` |
| **QQ 机器人** | ⚠️ 未实现 | 官方 API v2 用 Ed25519 签名（`X-Signature-Ed25519`），消息格式与发送接口都与微信不同，需另写适配层 |
| **个人微信（私人号）** | ❌ 不支持，也不建议 | 官方没有开放接口，只能 hook/协议库，封号与合规风险高 |

> 架构上平台相关代码被隔离在 `app/wechat/`（签名加解密、XML、发消息 API），
> 核心（DeepSeek 调用、会话记忆、限流、四种回复策略）与平台无关，所以加企业微信这类同构平台成本很低。

---

## 1. 这个方案解决了什么问题

微信服务器推送用户消息后，要求你的服务 **5 秒内**返回，否则：

1. 用户看到「该公众号暂时无法提供服务」；
2. 微信会**重试 3 次**（同一 `MsgId`），处理不当就会重复扣 AI 费用、重复回复。

而 DeepSeek 生成一段回答通常要 3~10 秒，直接同步返回必然超时。本项目的做法：

```
用户发消息
   │
   ▼
微信服务器 ──HTTP POST──▶ /wechat
   │                        │
   │                        ├─ 1. 校验签名 / 解密（安全模式）
   │                        ├─ 2. MsgId 去重（防重试重复消费）
   │                        ├─ 3. 限流判断
   │                        ├─ 4. 并发调用 DeepSeek（带会话记忆）
   │                        │
   │   ◀── 5 秒内返回 ─────┤  A. 抢在 3.5 秒内答完 → 直接被动回复
   │                        │  B. 没答完 → 先回「正在思考中…」，业务继续在后台跑
   │                        │
   │   ◀── 客服消息推送 ────┘  C. 生成完成后用客服消息接口把答案补发给用户
   ▼
用户看到答案
```

四种回复策略（`REPLY_MODE`）见 [第 5 节](#5-回复策略为什么默认会发两条消息)。

---

## 2. 目录结构

```
wechat-deepseek/
├── app/
│   ├── config.py           # 全部配置（读 .env / 环境变量）
│   ├── main.py             # FastAPI 入口：GET/POST /wechat、/healthz
│   ├── pipeline.py         # 核心流程：四种回复策略、去重、限流、调度
│   ├── deepseek.py         # DeepSeek 客户端（OpenAI 兼容协议，含 mock）
│   ├── memory.py           # 会话记忆(SQLite) / MsgId 去重 / 限流
│   ├── textutil.py         # 按字节截断、去 Markdown、日志脱敏
│   └── wechat/
│       ├── crypto.py       # 签名校验 + AES-256-CBC 加解密（安全模式）
│       ├── xml.py          # 微信 XML 解析与构造
│       └── api.py          # access_token 缓存 + 客服消息推送
├── tools/
│   ├── fake_wechat.py       # 模拟微信服务器，本地/线上联调必备
│   ├── check_credentials.py # 凭据体检：AppID/Secret、IP 白名单、客服消息通道、DeepSeek
│   ├── show_recent.py       # 查看线上真实对话记录（openid 默认脱敏）
│   ├── tail_messages.py     # 实时消息查看器（带时间戳，供 wxbot/messages.cmd 使用）
│   └── notify.py            # 往微信推运维提醒（地址变更等）
├── tests/                  # 39 个测试：加解密、端到端、回复策略
├── deploy/                 # nginx.conf、systemd service
├── Dockerfile / docker-compose.yml
├── run.py / run.sh / run.ps1
│
├── 一键脚本（Windows 双击即可，.cmd 不受 PowerShell 执行策略限制）
│   ├── configure.cmd       # 填入 AppID/AppSecret/DeepSeek Key 并自动重启+自检
│   ├── start.cmd           # 后台常驻启动（关掉终端也不停）
│   ├── stop.cmd            # 停止后台服务（按 PID 树 + 端口双重清理）
│   ├── start-tunnel.cmd    # 免注册拿公网 HTTPS 地址（微信后台要填的 URL）
│   ├── stop-tunnel.cmd     # 关闭隧道
│   └── run.cmd             # 前台运行，方便看日志
│
├── requirements.txt
└── .env.example            # 复制成 .env 后填写
```

> ⚠️ **Windows 两个坑，脚本里已经处理好了，但你改脚本时要记住：**
> 1. `.ps1` 文件必须存成 **UTF-8 with BOM**。Windows PowerShell 5.1 读无 BOM 的 `.ps1` 时会按 GBK 解码，
>    中文注释会让脚本直接报 `Missing property name after reference operator` 之类的语法错误。用记事本另存为「UTF-8 带 BOM」即可。
> 2. 默认执行策略是 `Restricted`，`.\run.ps1` 会被拒绝执行，所以请用 `.cmd` 包装（内部已带 `-ExecutionPolicy Bypass`），
>    或者手动 `powershell -ExecutionPolicy Bypass -File .\run.ps1`。

---

## 3. 五分钟本地跑通（不需要公众号）

### 3.0 最省事的用法：双击开关（本地模式）

> **操作三条铁律（血泪经验）：**
> 1. **同一时间只开一个 `wxbot.cmd` 窗口**。开多个窗口会互相抢同一个子域名，
>    报错长得像 `remote port forwarding failed for listen port 80`。
> 2. **关掉窗口后不要立刻重开**。隧道服务端（serveo）回收子域名有延迟，
>    立刻重开会暂时抢不到名字 —— 启动器每 15 秒会自动重试，等一会儿就好。
> 3. **抢不到名字时不要反复重启**，每重启一次就多一条待回收的连接，只会更糟。
>    实在抢不到就换一个没用过的名字（改 `.env` 的 `TUNNEL_SUBDOMAIN`）—— 新名字必然可用。

**双击 `wxbot.cmd`** —— 它会拉起机器人和公网隧道，并在窗口里实时显示状态和"后台该填什么"；
**关掉窗口（或 Ctrl+C）就全部停止**，包括强行结束窗口的情况。窗口内容（刻意全 ASCII，
避免部分终端把中文宽字符显示/复制成重复字符）：

```
============================================================
 WeChat x DeepSeek bot  |  running locally
============================================================

  Service  : [OK]   http://127.0.0.1:8000
  Tunnel   : [OK]   public reachable
             https://<你的子域名>.serveousercontent.com
  Provider : serveo   subdomain: <你的子域名>

  >> WeChat backend URL (server config) <<
     https://<你的子域名>.serveousercontent.com/wechat
  >> Token <<
     <你的Token>

  Stop     : close this window  (or press Ctrl+C)
```

### 3.0.1 在窗口里看用户消息（带时间）

`wxbot.cmd` 启动时会自动挂一个消息视图：用户发来的每条消息和 AI 的回复都会**带时间戳**实时打在同一个窗口里，
同时追加写入 `logs\messages.log`（UTF-8，终端显示异常时用记事本打开这个文件看）。

```
[20:00:41] 收到 on6wW3***-P0(28): 你好
[20:00:41] 回复: 你好呀！很高兴见到你。有什么问题想问我吗？不管是生活小疑惑
         、学习问题还是随便聊聊，都可以跟我说～
```

- 长消息按**显示宽度**折行（中文按 2 列算），不会挤成一片。
- openid 默认脱敏成 `on6wW3***-P0` 这种形式；想看完整 openid 用下面的 `messages.cmd --full`。
- 不想要这个视图：`wxbot.cmd -NoMessages`。

**单独开一个窗口看消息**（只读，不影响正在运行的机器人）：

```powershell
.\messages.cmd                  # 最近 20 条，然后持续跟随
.\messages.cmd --once --last 50  # 只看历史 50 条，不跟随
.\messages.cmd --full           # openid 不脱敏
```

### 3.0.2 人工主动回复（reply.cmd）

双击 `reply.cmd` 打开一个交互控制台，以公众号身份**手动**回用户（和 `wxbot.cmd` 并行运行、互不干扰）：

```
==========================================================
 人工主动回复控制台   (/quit 退出，空行取消)
==========================================================
最近给你发过消息的人：
  1)   on6wW3***-P0(28)       最后 10-10 20:08  「？」
  2)   on6wW3***BSs(28)       最后 10-10 21:31  「你好」

默认回复最近发消息的那个人（编号 1）。用 /to N 切换。

消息> 收到，我看一下
发送给 on6wW3***-P0(28) ...
  ✓ 已发送（对方微信里会收到）
```

| 命令 | 作用 |
| --- | --- |
| 直接输入文字 | 发给当前收件人 |
| `/to N` | 切换收件人（列表编号） |
| `/users` | 重新列出最近发过消息的人 |
| `/followers` | 列出所有关注者 |
| `/all` | 给所有关注者群发（要二次确认） |
| `/full` | 切换 openid 是否脱敏 |
| `/quit` | 退出 |

命令行参数：`reply.cmd --openid on6wW3...`（直接指定收件人）、`reply.cmd --dry-run`（只演示不发送）、
`reply.cmd --no-record`（不把手动回复写进会话记录）。

注意事项：

- 有 **48 小时窗口**：对方超过 48 小时没和公众号互动，微信会拒收（`45015`），列表里会用 `⚠` 标出来。
- 你手动发出的内容会写入会话库（记成 assistant），这样 AI 后续回复能"记得"你说过这句；不想要就加 `--no-record`。
- **以 `/` 开头的内容不会被误当命令发出去**：未知命令会被拦下并提示；确实要发以 `/` 开头的文本，写成 `//开头`。
- 输入里若带 BOM/零宽字符（复制粘贴常见）会被自动清理 —— 这是踩过坑之后加的防护。
- 想先确认要发什么，用 `--dry-run`。

关于地址：

- 用 **serveo 的保留子域**（`.env` 里 `TUNNEL_PROVIDER=serveo` + `TUNNEL_SUBDOMAIN=xxx`）时地址**固定不变**，
  公众号后台只需要填一次，之后开关窗口都不影响。
- 用 localhost.run 或 serveo 的随机地址时，地址每次启动都会变，后台要跟着改；
  脚本会在地址变化时尝试用客服消息把新地址推到你微信（`NOTIFY_OPENIDS` 配好收件人，见第 7 节）。
- 想彻底摆脱隧道：Cloudflare Tunnel + 自己的域名（¥30/年，本地开关机、地址永久固定），
  或直接上云服务器常驻（第 6 节）。

"关掉就停干净"是靠 **Windows Job Object（KILL_ON_JOB_CLOSE）** 实现的：
父进程无论怎么消失（关窗口、任务管理器结束、崩溃），内核都会连带清掉 python 与 ssh 子进程。
（实测：强杀启动器后 `python`/`ssh` 进程数归零、8000 端口释放。）

下面是手动跑通流程。

先确认整条链路没问题，再去折腾公众号后台。

```bash
cd wechat-deepseek
cp .env.example .env          # Windows: Copy-Item .env.example .env

# 打开 .env，至少改这几行：
#   WECHAT_TOKEN=any-local-token      # 本地随便设
#   DEEPSEEK_MOCK=1                   # 先用模拟模式，不消耗 API 额度
```

启动服务（二选一）：

```bash
# 方式一：一键脚本（自动建虚拟环境、装依赖）
./run.sh                # Linux/macOS
run.cmd                 # Windows 双击即可（比 .\run.ps1 更稳，见上面两个坑）

# 方式二：手动
python -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python run.py
```

想让服务在后台常驻（关掉终端也不停）：

```bash
start.cmd               # Windows：后台启动并写 logs\server.pid
stop.cmd                # 停止
# Linux：用 deploy/wechat-deepseek.service（systemd），见第 6 节
```

看到 `监听地址 : http://0.0.0.0:8000` 后，**另开一个终端**模拟微信服务器发消息：

```bash
python tools/fake_wechat.py --url http://127.0.0.1:8000/wechat --token any-local-token \
    --text "你好，介绍一下你自己"
```

预期输出：

```
[URL 校验] status=200 返回='...' => [OK] 通过
[发送] 「你好，介绍一下你自己」 => status=200 耗时=0.07s
[回复] MsgType=text Content='【MOCK 回复】我收到了你的消息：「你好，介绍一下你自己」'
```

想连续对话（验证多轮记忆）就不加 `--text`，会进入交互模式。
本地默认就是**明文模式**；安全模式（AES）这样测：

```bash
python tools/fake_wechat.py --url http://127.0.0.1:8000/wechat --token any-local-token \
    --aes-key <43位EncodingAESKey> --appid <AppID> --text "安全模式测试"
```

（`.env` 里的 `WECHAT_AES_KEY` 要填同一个值。）

跑自动化测试：

```bash
./.venv/bin/pip install -r requirements-dev.txt
./.venv/bin/python -m pytest tests -q      # 39 passed
```

---

## 4. 接入真实公众号

### 4.1 先搞清楚你的号能不能接

> ⚠️ 看过第 0 节的「支持范围」：**本项目只在测试号上做过端到端实测**。
> 下表是"平台能力"（微信允许你做什么），不是"本项目验证状态"。

| 账号类型 | 能否配置「服务器配置」 | 能否用客服消息推送 | 建议 |
| --- | --- | --- | --- |
| **测试号**（扫码即得） | ✅ | ✅ | **本项目实测过的就是它**，个人开发者首选 |
| 认证服务号 / 认证订阅号 | ✅ | ✅ | 生产环境用它（需企业主体）；协议同构但**未实测**，注意 IP 白名单与安全模式 |
| 未认证个人订阅号 | ❌ 后台没有这个入口 | ❌ | 只能用测试号，或做企业主体认证 |

> 公众号后台的「自动回复」只能发固定文本/关键词，**无法调用 AI**，所以想接 DeepSeek 必须走开发者模式。
> 个人开发者请直接申请测试号：<https://mp.weixin.qq.com/debug/cgi-bin/sandbox?t=sandbox/login>（微信扫码即可，`appID`/`appsecret` 当场给出）。

### 4.1.1 测试号能用什么、不能用什么（本项目实测边界）

测试号是**开发调试用**的临时环境，把它当正式号用会踩坑。下面区分"我们实测过的"和"平台机制决定的"：

**✅ 实测可用（本项目就是靠这些跑通的）**

| 能力 | 说明 |
| --- | --- |
| 服务器配置（URL + Token） | 填完点提交即生效，微信真实回调校验通过 |
| 接收用户消息 | 真实用户发消息能收到（文本） |
| 被动回复 | 5 秒内单条回复，实测响应 0.7~2.2 秒 |
| 客服消息推送 | 实测成功（`errcode=0`），这是"超过 3.5 秒先回 ACK 再补发"的依赖 |
| `access_token` | 可用，无需 IP 白名单（本项目从未触发 `40164`） |

**⚠️ 平台机制决定的限制（不是本项目的问题）**

| 限制 | 对本项目的影响 |
| --- | --- |
| 只能扫码关注「测试号二维码」，微信里搜不到这个名字 | 只能自己/家人用，**无法对外获客** |
| 后台**没有 EncodingAESKey 配置项** | 只能用**明文模式**；安全模式（AES）在测试号上无法配置（本项目只在模拟请求里验证过 AES） |
| 没有微信认证，因此没有素材/群发/支付/网页授权等能力 | 不能当内容号运营，只能做对话 |
| 官方定位就是"开发调试"，可能调整规则或清理 | **不要用于生产**；对外服务请走认证号 |
| 关注人数、有效期等硬性数字以你后台页面显示为准 | 本项目不写死数字，避免误导 |

> 想确认自己测试号的具体权限：打开测试号页面看那张「**接口权限表**」，它能调用的接口一目了然。
> 只要里面**没有**你要用的接口（比如群发、素材），那就是平台不给，不是配置问题。

### 4.2 拿到 DeepSeek API Key

1. 打开 <https://platform.deepseek.com>，注册/登录；
2. 左侧 **API keys** → **创建 API key** → 复制 `sk-xxxxxxxx`；
3. 到 **充值** 页面按量充值（`deepseek-chat` 很便宜，日常对话几块钱能用很久）。

填进 `.env`：

```ini
DEEPSEEK_API_KEY=sk-你的key
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_MOCK=0
```

### 4.3 让服务暴露到公网

微信**必须能访问到你的地址**，且有两条硬要求：

- URL 以 `http://` 或 `https://` 开头，**只支持 80 和 443 端口**（不能写 `:8000`）；
- 正式号建议使用**已备案的域名**；测试号一般可用免费二级域名。

**方式 A：内网穿透（本机开发，最快）**

本仓库自带一个**免注册**的隧道脚本，出口是 443 HTTPS，直接符合微信要求：

```bash
start-tunnel.cmd        # Windows：自动建立隧道并打印公网地址
stop-tunnel.cmd         # 关闭隧道
# Linux/macOS 等价命令：
#   ssh -o StrictHostKeyChecking=no -R 80:127.0.0.1:8000 nokey@localhost.run
```

输出示例：

```
公网地址： https://13d51c076c90ec.lhr.life
后台填写： https://13d51c076c90ec.lhr.life/wechat
Token  ： <你的Token>
```

> ⚠️ **必须写 `127.0.0.1` 而不是 `localhost`。** Windows 上 `localhost` 会优先解析成 IPv6 `::1`，
> 而 uvicorn 默认只监听 IPv4，症状是隧道地址能解析但请求一律失败
> （`curl: (52) Empty reply from server` / `Server disconnected without sending a response`）。

> ⚠️ **匿名隧道是短命的。** `localhost.run` 的匿名隧道实测会在几十分钟内被对端回收
> （症状：公网地址返回 `no tunnel` / HTTP 502，`ssh` 进程消失）。用来当场验证链路没问题，
> 但**别指望它长期稳定**。做到下面任一条才适合长期跑：
> 1. 在 localhost.run 注册并上传 SSH 公钥，绑定固定子域（免费）；
> 2. 换 cpolar / natapp 的固定隧道；
> 3. 用 frp + 自己的域名（最稳，见方式 B）；
> 4. 直接上云服务器（推荐）。
>
> 隧道挂了公众号会提示"该公众号暂时无法提供服务"，重新跑 `start-tunnel.cmd` 会得到**新地址**，
> 记得回后台改 URL。

#### 免费固定子域：注册 SSH 公钥（推荐，5 分钟）

注册后子域不再随机、隧道寿命明显变长，配合脚本的自愈模式基本可以长期挂着。

**第 1 步：拿到本机公钥**

```powershell
Get-Content "$env:USERPROFILE\.ssh\dsh_localhost_run.pub"
```

没有这个文件就先跑一次 `start-tunnel.cmd`（脚本不依赖密钥也能建隧道），再执行：

```powershell
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\dsh_localhost_run" -N '""' -C "wechat-deepseek-bot"
```

**第 2 步：上传公钥**

打开 <https://admin.localhost.run/>（GitHub 或邮箱登录）→ 把上面那一整行 `ssh-ed25519 AAAA...`
粘进去保存，并在后台设置你想要的**子域名**（例如 `wxai` → `https://wxai.lhr.life`）。

**第 3 步：重新建隧道，确认地址固定**

```powershell
.\start-tunnel.cmd          # 默认带 -Watch 自愈模式
```

之后每次重连都会拿到**同一个地址**，后台 URL 只需配一次。

关于自愈：`start-tunnel.ps1 -Watch` 每 30 秒用 `curl` 探一次公网 `/healthz`，
而不是只看 `ssh` 进程是否存活 —— 实测出现过"进程还在、对端已回收隧道"的情况，只看进程会漏判。
只想建一次就退出用 `.\start-tunnel.ps1`（不带 `-Watch`）。

**开机自启（可选）**

按 `Win+R` 输入 `shell:startup` 打开启动文件夹，把这两个快捷方式丢进去：

- `start.cmd`（后台拉起机器人）
- `start-tunnel.cmd`（拉起隧道并自愈）

也可以用任务计划程序设为"登录时运行"。

也可以用 cpolar / natapp / frp，命令类似：

```bash
cpolar http 8000        # 免费版给出 https://xxxx.r6.cpolar.top
```

注意：免费隧道的域名在重启后会变，变了就要回后台改 URL；
长期使用建议用 frp + 自己的域名（80/443）。
另外**部分免费隧道域名可能不被微信服务器接受**，如果后台提交时报「该地址不支持」或一直验证失败，
请换成自有域名的方案（见方式 B）。

**方式 B：云服务器 + Nginx + HTTPS（推荐长期使用）**

见 [第 6 节](#6-部署到云服务器)。

### 4.4 公众号后台配置（测试号 / 正式号都一样）

在 **设置与开发 → 基本配置 → 服务器配置** 里填：

| 字段 | 填什么 |
| --- | --- |
| URL | `https://你的域名/wechat` |
| Token | 与 `.env` 的 `WECHAT_TOKEN` **完全一致** |
| EncodingAESKey | 点「随机生成」，想用安全模式就把它复制到 `.env` 的 `WECHAT_AES_KEY` |
| 消息加解密方式 | 建议先选**明文模式**跑通，再切**安全模式** |
| 数据格式 | JSON 或 XML 都可以（本项目按 XML 处理推送消息） |

点「提交」时，微信会立刻发一个 GET 校验请求，**服务必须是运行状态**，通过后点「启用」。

> ⚠️ 启用服务器配置后，后台原本设置的「自动回复」会失效，这是正常的。

最后把 `WECHAT_APPID` / `WECHAT_APPSECRET` 也填进 `.env`（客服消息推送需要），重启服务：

```bash
./.venv/bin/python run.py
# 日志应显示：客服推送=可用
```

然后**关注公众号（测试号要扫页面上的二维码）→ 发一句话 → 收到 AI 回复**。

### 4.5 别忘了 IP 白名单

微信 **基本配置 → IP 白名单** 里，把你服务器的**公网出口 IP** 加进去，
否则调用 `access_token` / 客服消息会报 `40164`（invalid ip）。

---

## 5. 回复策略：为什么默认会发两条消息

`REPLY_MODE` 控制怎么应对 5 秒限制：

| 模式 | 行为 | 用户体验 | 适用 |
| --- | --- | --- | --- |
| `hybrid`（默认） | 先等 `PASSIVE_TIMEOUT`(3.5s)：答完就正常回复；没答完先回「正在思考中…」，答案稍后由客服消息推来 | 可能有两条消息 | 通用推荐，响应最快 |
| `instant_ack` | 立刻回「正在思考中…」，答案一律推送 | 稳定两条 | DeepSeek 慢（如 `deepseek-reasoner`）时 |
| `push` | 不做被动回复，只用客服消息把答案推过去 | 只有一条，但第一秒没反馈 | 只想要干净结果 |
| `passive` | 只被动回复，超时回固定兜底文案 | 超时会丢答案 | 没配 `AppSecret`、拿不到客服接口权限时 |

想只要一条消息又要稳定，就把 `PASSIVE_TIMEOUT` 调大试试（例如 `4.5`），
但风险是逼近微信 5 秒红线，**不建议超过 4.5**。

---

## 6. 部署到云服务器

### 6.1 先买对（这一步错了后面全是坑）

| 项目 | 建议 | 原因 |
| --- | --- | --- |
| 规格 | 1 核 1G / 20G 盘 / 1~3Mbps | 这个服务极轻，最低配足够 |
| 系统 | Ubuntu 22.04 / 24.04 或 Debian 12 | `deploy/bootstrap.sh` 针对这三个验证过命令 |
| 地区 | **香港 / 新加坡 / 日本等境外节点** | **免 ICP 备案，80/443 直接用**。国内节点用域名开 80/443 必须先备案（1~3 周） |
| 参考价 | 香港轻量约 ¥24~40/月；Vultr / RackNerd 年付约 $15~25 | 比 localhost.run 的 $9/月固定域名方案便宜 |
| 域名 | 任意注册商，加一条 **A 记录指向 VPS 公网 IP** | 微信后台要填域名 |

> 微信「服务器配置」只接受 **80/443** 端口，所以别买只能开非标准端口的方案。

### 6.2 一键部署（推荐）

买好 VPS、把域名 A 记录指过去之后，在**本机项目根目录**执行：

```powershell
.\deploy\upload-and-deploy.ps1 -Server root@1.2.3.4 -Domain wx.example.com -Email me@example.com
```

它会自动：打包项目（排除 `.venv`/`logs`/`data`）→ scp 上传 → 远程解包 → 跑 `deploy/bootstrap.sh`，
后者会装依赖、建虚拟环境、写 systemd 服务、配 Nginx 反代、用 certbot 签 HTTPS 并强制跳转，
最后打印微信后台该填的 URL。中途若提示密码，输入 VPS 登录密码即可（脚本自带 `accept-new`，不会卡在指纹确认）。

部署完成后，微信后台配置改成：

```
URL   : https://wx.example.com/wechat
Token : 与 .env 的 WECHAT_TOKEN 一致
```

以后维护：

```bash
systemctl status wechat-deepseek     # 状态
journalctl -u wechat-deepseek -f     # 实时日志
systemctl restart wechat-deepseek    # 重启（改完 .env 要重启）
```

改了代码要重新上线，再跑一次 `upload-and-deploy.ps1` 即可（幂等）。

### 6.3 手动部署（想自己掌控每一步时用）

以 Ubuntu 22.04 为例（1 核 1G 足够）。

```bash
# 1) 环境
sudo apt update && sudo apt install -y python3-venv nginx
sudo mkdir -p /opt/wechat-deepseek && sudo chown $USER /opt/wechat-deepseek

# 2) 上传代码（git clone 或 scp 整个目录到 /opt/wechat-deepseek）
cd /opt/wechat-deepseek
cp .env.example .env && vim .env        # 填 Token / AppID / AppSecret / DeepSeek Key

# 3) 装依赖并先手动跑一遍
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python run.py               # 确认无报错后 Ctrl+C

# 4) 交给 systemd 常驻（记得改 service 里的路径与 User）
sudo cp deploy/wechat-deepseek.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wechat-deepseek
sudo journalctl -u wechat-deepseek -f

# 5) Nginx 反向代理 + HTTPS 证书
sudo cp deploy/nginx.conf /etc/nginx/conf.d/wechat.conf   # 先改里面的域名和证书路径
sudo certbot --nginx -d wx.example.com                    # 自动签发并续期
sudo nginx -t && sudo systemctl reload nginx
```

配好后公网访问 `http://你的域名/healthz`，应返回 `{"status":"ok",...}`。

### Docker 部署（可选）

```bash
cp .env.example .env && vim .env
docker compose up -d --build
docker compose logs -f
# 容器只监听 127.0.0.1:8000，对外仍由宿主机 Nginx 提供 443
```

---

## 7. 配置项速查（`.env`）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `WECHAT_TOKEN` | — | **必填**，与后台服务器配置里的 Token 一致 |
| `WECHAT_APPID` / `WECHAT_APPSECRET` | — | 客服消息推送必需；不填则只能用被动回复 |
| `WECHAT_AES_KEY` | 空 | 43 位 EncodingAESKey，仅安全模式需要 |
| `DEEPSEEK_API_KEY` | — | **必填**（`DEEPSEEK_MOCK=1` 时可空） |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 或 `deepseek-reasoner`（更慢更贵） |
| `DEEPSEEK_TIMEOUT` | `45` | 单次调用超时秒数，reasoner 建议 `90` |
| `DEEPSEEK_SYSTEM_PROMPT` | 内置 | 人设/风格，内置提示词已要求「少用 Markdown、控制长度」 |
| `DEEPSEEK_MOCK` | `0` | `1`=不调用 API，直接回显，用于本地联调 |
| `REPLY_MODE` | `hybrid` | 见第 5 节 |
| `PASSIVE_TIMEOUT` | `3.5` | 被动回复等待上限，勿超 4.5 |
| `MAX_REPLY_BYTES` | `1800` | 微信文本上限 2048 字节，留余量 |
| `MAX_INPUT_CHARS` | `800` | 超长用户输入截断 |
| `STRIP_MARKDOWN` | `1` | 微信不渲染 Markdown，默认降级成纯文本 |
| `HISTORY_TURNS` | `6` | 记忆轮数，`0`=无记忆（省 token） |
| `SESSION_TTL` | `3600` | 会话有效期（秒） |
| `RATE_LIMIT_SECONDS` / `RATE_LIMIT_PER_HOUR` | `1.0` / `60` | 单用户最小间隔与每小时上限，防刷 |
| `MAX_CONCURRENCY` | `4` | 同时进行的 DeepSeek 请求数 |
| `DB_PATH` | `data/sessions.db` | 会话存储位置 |
| `PORT` | `8000` | 本地监听端口（对外仍须 80/443） |

---

## 8. 联调与排错

### 8.1 用工具定位问题

```bash
# 只测 URL 校验（对应后台点"提交"那一步）
python tools/fake_wechat.py --url https://你的域名/wechat --token xxx --check-only

# 完整发一条消息
python tools/fake_wechat.py --url https://你的域名/wechat --token xxx --text "测试"
```

服务端日志（`journalctl -u wechat-deepseek -f` 或控制台）会打印收到什么类型、耗时多少。

### 8.2 常见报错对照表

| 现象 / 错误码 | 原因 | 处理 |
| --- | --- | --- |
| 后台提示「token验证失败」 | URL 或 Token 与 `.env` 不一致 | 逐字符核对；URL 必须带 `/wechat` |
| 后台提示「该地址不支持」 | 端口不是 80/443，或发生跳转 | 用 80/443；Nginx 不要 302 到别的路径 |
| 用户看到「该公众号暂时无法提供服务」 | 被动回复超 5 秒（`45015`） | 配好 `APPID`/`APPSECRET` 启用客服推送；或 `DEEPSEEK_MODEL=deepseek-chat` |
| `40001` invalid credential | AppSecret 错误 / token 失效 | 核对 `WECHAT_APPSECRET`；程序会自动强刷一次 |
| `40164` invalid ip | 服务器 IP 不在白名单 | 基本配置里加入公网出口 IP |
| `48001` api unauthorized | 账号类型没有该接口权限 | 未认证号无法用客服消息；改用测试号或认证号 |
| `45015` 客服消息下发失败 | 用户超过 **48 小时**没和公众号互动（或已取关） | 不是配置问题：真实对话中用户刚发完消息，窗口是开的；主动推送前必须等用户先说话 |
| `45047` 客服接口下行超限 | 48 小时内推送太多 | 降低 `push_max_chunks` / 减少主动推送 |
| 日志 `signature 校验失败` | Token 与后台不一致，或安全/明文模式不匹配 | 核对 Token 与「消息加解密方式」 |
| 用户收到两条消息 | `hybrid` 策略先回了「正在思考中…」 | 换 `REPLY_MODE=push` 可只发一条 |
| 回复里出现 `**`、`#` | 模型仍输出了 Markdown | 确认 `STRIP_MARKDOWN=1`（默认开） |

---

## 9. 安全、合规与成本

- **不要提交 `.env`**：`.gitignore` 已排除，AppSecret / API Key 泄露等于账号和钱包一起送人。
- **公网只开 80/443**：应用本身监听 `127.0.0.1:8000`（Nginx 反代），不要直接把 8000 暴露到公网。
- **接口已做防护**：签名校验（`signature` / `msg_signature`）、消息体上限、MsgId 幂等去重、单用户限流、日志 openid 脱敏。
- **会话数据**：默认存本地 SQLite（`data/sessions.db`），只保存最近若干轮；不想留痕就设 `HISTORY_TURNS=0`，并定期删除 `data/`。
- **成本控制**：`HISTORY_TURNS` 越大越贵；`DEEPSEEK_MAX_TOKENS=800` 已够日常问答；`deepseek-reasoner` 会明显更慢更贵。
- **内容合规**：公众号面向公众，建议加敏感词过滤/人工兜底；DeepSeek 的输出不保证 100% 准确，涉及医疗、金融等场景请加免责声明。
- **多实例部署**：MsgId 去重与限流是**进程内**的，`workers>1` 时偶发重复回复。要保持单进程，或把 `SeenCache`/`RateLimiter` 换成 Redis（`app/memory.py` 里改）。

---

## 10. 常见问题

**Q：我有一个个人主体的订阅号，能接吗？**
A：**不能**。判断方法很简单 —— 打开公众号后台，左侧有没有「设置与开发 → 基本配置 → 服务器配置」：

- 有 → 可以接（认证服务号 / 认证订阅号 / 测试号）；
- 没有 → 接不了，这不是代码问题。典型就是**未认证的个人主体订阅号**：微信只对已完成微信认证的账号开放开发者模式，
  而微信认证只面向组织主体（企业 / 个体户 / 其他组织），个人主体拿不到。
  这类账号只能用「自动回复（固定文本/关键词）」「自定义菜单」「发文章」，**无法调用 AI**。

现实的三条路：① 继续用**测试号**（本项目实测的就是它，零成本，适合自己/家人用）；
② 用个体户或企业主体注册并认证（¥300/年）；③ 企业微信（免费，但本项目尚未实现 WeCom 适配，见第 0 节）。

**Q：能让它只发一条消息吗？**
A：`REPLY_MODE=push`（不被动回复，直接推送答案），或把 `PASSIVE_TIMEOUT` 提到 `4.5` 赌一把。

**Q：能流式输出吗？**
A：不能。公众号被动回复与客服消息都是整条下发，没有流式通道；本项目的「正在思考中…」已经是能给的最快反馈。

**Q：支持图片、语音、位置消息吗？**
A：目前文本以外的消息会回一句提示。要支持语音，需要先调微信语音识别接口（认证服务号 + 语音识别权限），或自己接 ASR。

**Q：怎么换成自己的知识库 / 让 AI 只回答业务问题？**
A：改 `DEEPSEEK_SYSTEM_PROMPT` 做人设约束；要做 RAG 就在 `app/pipeline.py` 的 `_answer()` 里，把检索结果拼进 `messages`。

**Q：怎么改欢迎语、增加关键词回复、加自定义菜单？**
A：欢迎语在 `_handle_event()` 的 `subscribe` 分支（`WELCOME_TEXT`）；关键词回复在 `handle()` 里加分支即可；自定义菜单用公众号后台或「自定义菜单」接口配置。

**Q：想加人工客服兜底（比如用户发「转人工」）？**
A：在 `handle()` 里拦截关键词，改走客服消息另一个分支或标记会话状态即可，代码结构已经留好了位置。

---

## 11. 已验证内容

本仓库交付前实际执行过（不是"应该能跑"）：

**自动化测试**

- `python -m pytest tests -q` → **39 passed**：签名校验、AES 加解密往返、错误 key 拒绝、XML 解析、
  文本截断/切分、会话存储与 TTL、限流与去重、端到端明文/安全模式消息、四种回复策略。

**真实 HTTP 联调（本机 + 公网隧道）**

- 启动真实服务，用 `tools/fake_wechat.py` 模拟微信服务器走 **公网 HTTPS 隧道**：
  - `/healthz` → 200；
  - URL 校验 → 200 并原样返回 echostr（错误 Token → 403）；
  - 明文模式消息 → 200，收到正确回复；
  - **安全模式（AES）消息 → 200**，密文双向加解密正确，回复的 `MsgSignature` 可被校验通过；
- 超长输入与超长回复按字节截断（不会切坏汉字）、非文本消息提示、重复 `MsgId` 去重、
  畸形 XML 不崩溃（回 `success` 而不是 500）。

**联调中发现并修掉的真实问题**

| 问题 | 症状 | 修法 |
| --- | --- | --- |
| Windows 控制台 GBK | 工具里的 emoji 让进程直接抛 `UnicodeEncodeError` | 输出改 UTF-8 + ASCII 标记 |
| `.ps1` 无 BOM | PowerShell 5.1 按 GBK 解析中文注释，报 `Missing property name` | 所有 `.ps1` 存成 UTF-8 with BOM |
| 执行策略 Restricted | `.\run.ps1` 被拒绝执行 | 增加 `.cmd` 包装（内部 `-ExecutionPolicy Bypass`） |
| venv python 是转发进程 | `stop.ps1` 停了但端口仍被占用 | 改为 `taskkill /T` + 按端口兜底 |
| 隧道用 `localhost` | 公网请求 `Empty reply from server` | 改 `127.0.0.1`，避开 IPv6 |
| 隧道脚本读日志未判空 | 首轮 `[regex]::Match($null)` 异常中断脚本 | 先判空再匹配 |

**真实公众号联调（在「公众号测试号」上用真实 AppID/AppSecret/关注者验证过）**

> 再次强调：以下实测**全部发生在测试号**上，正式号（认证服务号/订阅号）尚未验证。

- `access_token` 获取成功 → AppID/AppSecret 正确、IP 白名单未拦截；
- 后台「服务器配置」提交后，日志出现 `URL 校验通过`（微信服务器真实校验，而非本地模拟）；
- **真实用户从微信发来「你好」，0.91 秒后收到 AI 回复**（走被动回复，5 秒内单条送达）；
- **客服消息推送通道实测成功**（`check_credentials.py` 向真实关注者发送测试消息返回 `errcode=0`），
  这条是"DeepSeek 生成超过 3.5 秒时先回 ACK 再补发答案"的关键依赖；
- 实测 DeepSeek 首轮响应 0.7~2.2 秒，绝大多数问题都走被动回复这条路，用户只看到一条消息。

**最终上线形态（本地开关 + serveo 固定子域，已用真实流量验证）**

- 隧道换成 **serveo 保留子域**（`TUNNEL_PROVIDER=serveo` + `TUNNEL_SUBDOMAIN=xxx`），地址固定为
  `https://xxx.serveousercontent.com`，公众号后台只填一次，之后开关窗口都不用再改；
- 双击 `wxbot.cmd` 启动（机器人 + 隧道），关掉窗口即全部停止（Job Object，强杀也干净）；
- 公众号后台提交后日志出现 `URL 校验通过`，随后**真实用户发「你好」，0.89 秒收到 AI 回复**，
  访问日志可见真实外部 IP（微信服务器网段）与真实 `openid=`；
- serveo 免费版会对**浏览器**请求插一个 `Serveo - Warning` 页，但**机器对机器的 webhook 流量直通**
  （已实测 URL 校验与消息收发均不受影响）—— 自己用浏览器打开那个域名看到警告页是正常的。

> 排查提示：流量经隧道转发时，服务端看到的来源 IP 可能是 `127.0.0.1`（取决于隧道实现），
> 所以**不要只靠 IP 判断一条消息是不是来自微信**。可靠特征是 `POST /wechat` 的查询串里带真实 `openid=`
> （形如 `on6wW3...`，28 位），以及 `tools/show_recent.py --users` 里出现非 `oTest*` 的用户。

**还没验证的环节**

免费隧道域名的长期稳定性（见 4.3 的警告）；以及如果换成正式号，需按 8.2 节排查表核对各 errcode。
