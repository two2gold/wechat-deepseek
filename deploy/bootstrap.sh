#!/usr/bin/env bash
# ============================================================================
# 一键部署：在全新的 Ubuntu 22.04/24.04 或 Debian 12 上把公众号机器人跑起来
#
# 用法（在 VPS 上、项目目录内执行）：
#     sudo bash deploy/bootstrap.sh wx.example.com you@example.com
#
# 它会做完这些事（可重复执行，幂等）：
#   1. 装 python3-venv / nginx / certbot
#   2. 把项目放到 /opt/wechat-deepseek（已在就别处会自动同步过去）
#   3. 建虚拟环境、装依赖
#   4. 写 systemd 服务并设为开机自启
#   5. 配 Nginx 反向代理（80 端口先跑通），再用 certbot 签 HTTPS 证书并强制跳转
#   6. 本地与公网自检，打印微信后台该填的 URL
# ============================================================================
set -euo pipefail

DOMAIN="${1:-}"
EMAIL="${2:-}"
if [[ -z "$DOMAIN" ]]; then
  echo "用法: sudo bash deploy/bootstrap.sh <域名> <邮箱>"
  echo "例如: sudo bash deploy/bootstrap.sh wx.example.com me@example.com"
  exit 1
fi

APP_DIR=/opt/wechat-deepseek
SERVICE=wechat-deepseek
RUN_USER=www-data
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log()  { echo -e "\033[32m[bootstrap]\033[0m $*"; }
warn() { echo -e "\033[33m[bootstrap]\033[0m $*"; }

if [[ $EUID -ne 0 ]]; then
  echo "请用 root 运行：sudo bash deploy/bootstrap.sh $DOMAIN ${EMAIL:-you@example.com}"
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------- 1. 依赖
log "安装系统依赖……"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip nginx curl ca-certificates >/dev/null
if ! command -v certbot >/dev/null 2>&1; then
  apt-get install -y -qq certbot python3-certbot-nginx >/dev/null
fi

# ------------------------------------------------------- 2. 同步项目到 /opt
if [[ "$SRC_DIR" != "$APP_DIR" ]]; then
  log "同步项目：$SRC_DIR -> $APP_DIR"
  mkdir -p "$APP_DIR"
  # 排除本地产物，避免把 Windows 的虚拟环境和日志带上去
  tar -C "$SRC_DIR" \
      --exclude='.venv' --exclude='logs' --exclude='data' \
      --exclude='__pycache__' --exclude='.pytest_cache' --exclude='*.pyc' \
      -cf - . | tar -C "$APP_DIR" -xf -
fi
cd "$APP_DIR"

if [[ ! -f .env ]]; then
  warn "没有找到 .env，先从模板创建，记得填 WECHAT_TOKEN / WECHAT_APPID / WECHAT_APPSECRET / DEEPSEEK_API_KEY"
  cp .env.example .env
fi

# ------------------------------------------------------------- 3. 虚拟环境
log "准备 Python 虚拟环境……"
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt
log "依赖安装完成（$(./.venv/bin/python -V)）"

mkdir -p data logs
chown -R "$RUN_USER:$RUN_USER" data logs
chmod 640 .env 2>/dev/null || true
chgrp "$RUN_USER" .env 2>/dev/null || true

# --------------------------------------------------------------- 4. systemd
log "写入 systemd 服务……"
cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=WeChat Official Account x DeepSeek bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/.venv/bin/python run.py
Restart=always
RestartSec=3
User=${RUN_USER}
Group=${RUN_USER}
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ReadWritePaths=${APP_DIR}/data ${APP_DIR}/logs
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE}

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1 || true
systemctl restart "$SERVICE"
sleep 4

# ------------------------------------------------------------------ 5. Nginx
log "配置 Nginx（域名：$DOMAIN）……"
cat > "/etc/nginx/sites-available/${SERVICE}" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};

    client_max_body_size 1m;

    location /wechat {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        # 微信只等 5 秒，Nginx 不要比它更有耐心，也不要缓冲
        proxy_connect_timeout 3s;
        proxy_send_timeout    10s;
        proxy_read_timeout    10s;
        proxy_buffering       off;
    }

    location /healthz {
        proxy_pass http://127.0.0.1:8000;
        access_log off;
    }

    location / { return 404; }
}
EOF
ln -sf "/etc/nginx/sites-available/${SERVICE}" "/etc/nginx/sites-enabled/${SERVICE}"
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

# 有些 VPS 默认开着 ufw，不放行的话公网访问不到
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow 'Nginx Full' >/dev/null 2>&1 || true
  log "检测到 ufw 已启用，已放行 Nginx Full（80/443）"
fi

# ------------------------------------------------------------------ 6. 自检
log "本地自检……"
if curl -fsS -m 8 http://127.0.0.1:8000/healthz >/dev/null; then
  log "机器人进程正常"
else
  warn "机器人没起来，看日志：journalctl -u ${SERVICE} -n 50 --no-pager"
fi

log "公网自检（HTTP）……"
HTTP_CODE="$(curl -s -o /dev/null -w '%{http_code}' -m 10 "http://${DOMAIN}/healthz" || true)"
if [[ "$HTTP_CODE" == "200" ]]; then
  log "http://${DOMAIN}/healthz -> 200，DNS 与反向代理都通了"
else
  warn "http://${DOMAIN}/healthz -> ${HTTP_CODE:-失败}；确认域名 A 记录已指向本机 IP、且 80 端口对公网开放"
fi

if [[ -n "$EMAIL" ]]; then
  log "申请 HTTPS 证书（Let's Encrypt）……"
  if certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$EMAIL" --redirect; then
    log "HTTPS 已启用并强制跳转"
  else
    warn "证书申请失败（常见原因：域名还没解析到本机 / 80 端口不通 / 申请次数超限）。"
    warn "修好后重跑这一条即可：certbot --nginx -d $DOMAIN -m $EMAIL --agree-tos --redirect"
  fi
else
  warn "没提供邮箱，跳过 HTTPS。之后执行：certbot --nginx -d $DOMAIN -m 你的邮箱 --agree-tos --redirect"
fi

# ------------------------------------------------------------ 7. 收尾提示
cat <<EOF

============================================================
 部署完成
============================================================
 服务状态 : systemctl status ${SERVICE}
 实时日志 : journalctl -u ${SERVICE} -f
 手动自检 : curl -s http://127.0.0.1:8000/healthz
 公网自检 : curl -s https://${DOMAIN}/healthz

 微信后台「服务器配置」填：
   URL   : https://${DOMAIN}/wechat
   Token : $(grep -E '^WECHAT_TOKEN=' .env | cut -d= -f2-)

 填完点「提交」，日志里应出现「URL 校验通过」。
============================================================
EOF
