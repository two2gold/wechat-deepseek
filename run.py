"""启动脚本：python run.py

等价于：uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import uvicorn

from app.config import settings


def main() -> None:
    print("=" * 62)
    print(" 微信公众号 × DeepSeek 服务")
    print(f"  监听地址 : http://{settings.host}:{settings.port}")
    print(f"  微信 URL : http://<你的公网域名>:{settings.port}/wechat")
    print(f"  健康检查 : http://127.0.0.1:{settings.port}/healthz")
    print(f"  DeepSeek : {settings.deepseek_model}（{'模拟模式' if settings.deepseek_mock else '正式调用'}）")
    print(f"  回复策略 : {settings.reply_mode}")
    print("=" * 62)
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        access_log=True,
    )


if __name__ == "__main__":
    main()
