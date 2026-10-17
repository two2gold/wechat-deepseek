#!/usr/bin/env python
"""给指定用户推一条客服消息（用于「隧道地址变了」这类运维提醒）。

收件人来自 .env 的 NOTIFY_OPENIDS（逗号分隔），也可以用 --openid 临时指定。
客服消息有 48 小时窗口限制：收件人超过 48 小时没和公众号互动就发不出去（errcode 45015），
所以这只是"尽力通知"，失败不影响主流程。

用法：
    .\\.venv\\Scripts\\python.exe tools\\notify.py --text "你好"
    .\\.venv\\Scripts\\python.exe tools\\notify.py --openid on6wW3... --text "你好"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.textutil import mask_id  # noqa: E402
from app.wechat.api import WeChatAPI, WeChatAPIError  # noqa: E402


def _utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


async def main() -> int:
    _utf8()
    parser = argparse.ArgumentParser(description="推送运维提醒")
    parser.add_argument("--text", required=True, help="消息内容")
    parser.add_argument("--openid", default="", help="临时指定收件人（默认用 .env 的 NOTIFY_OPENIDS）")
    args = parser.parse_args()

    targets = [args.openid] if args.openid else sorted(settings.notify_openids)
    if not targets:
        print("没有配置收件人：在 .env 里设置 NOTIFY_OPENIDS=<你的openid>（逗号分隔）")
        return 1

    api = WeChatAPI(settings.wechat_appid, settings.wechat_appsecret)
    ok = 0
    try:
        for openid in targets:
            try:
                await api.send_text(openid, args.text)
                ok += 1
                print(f"[OK]   {mask_id(openid)}")
            except WeChatAPIError as exc:
                hint = "（超过 48 小时未互动，微信不允许主动推送）" if exc.errcode == 45015 else ""
                print(f"[FAIL] {mask_id(openid)} errcode={exc.errcode} {hint}")
        return 0 if ok else 1
    finally:
        await api.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
