#!/usr/bin/env bash
# Linux / macOS 一键启动（首次会创建虚拟环境并装依赖）
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "缺少 .env：先执行 cp .env.example .env 并填写 WECHAT_TOKEN / DEEPSEEK_API_KEY"
  exit 1
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt
exec ./.venv/bin/python run.py
