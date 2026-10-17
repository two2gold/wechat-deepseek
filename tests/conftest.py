"""pytest 公共配置：必须在导入 app 之前把环境变量准备好。"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 43 位合法 EncodingAESKey（32 字节随机数的 base64 去掉 "="）
AES_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
TOKEN = "test-token"
APPID = "wx_test_appid"

os.environ.update(
    {
        "WECHAT_TOKEN": TOKEN,
        "WECHAT_APPID": APPID,
        "WECHAT_APPSECRET": "",  # 故意留空：测试中不访问微信外网接口
        "WECHAT_AES_KEY": AES_KEY,
        "DEEPSEEK_API_KEY": "",
        "DEEPSEEK_MOCK": "1",
        "REPLY_MODE": "hybrid",
        "PASSIVE_TIMEOUT": "3",
        "RATE_LIMIT_SECONDS": "0",
        "RATE_LIMIT_PER_HOUR": "1000",
        "DB_PATH": "data/test_sessions.db",
        "LOG_LEVEL": "warning",
    }
)
