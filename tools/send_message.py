#!/usr/bin/env python
"""主动给某个用户发一条客服消息（或先列出可发送的用户）。

前提：客服消息只能发给「48 小时内和公众号互动过」的用户，
超过窗口会返回 45015；已取关会返回其它错误码。

用法：
    .\\.venv\\Scripts\\python.exe tools\\send_message.py --list
    .\\.venv\\Scripts\\python.exe tools\\send_message.py --openid on6wW3... --text "你好"
    .\\.venv\\Scripts\\python.exe tools\\send_message.py --all --text "😄"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

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


async def fetch_followers(api: WeChatAPI) -> list[str]:
    """取关注者 openid 列表。测试号/未认证号可能没有这个接口权限，返回空列表。"""
    token = await api.access_token()
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            "https://api.weixin.qq.com/cgi-bin/user/get", params={"access_token": token}
        )
    data = resp.json()
    if data.get("errcode", 0) != 0:
        print(f"[提示] 取关者列表失败：errcode={data.get('errcode')} {data.get('errmsg')}")
        print("       这通常是因为账号没有「获取用户列表」接口权限，不影响单发客服消息。")
        return []
    followers = (data.get("data") or {}).get("openid") or []
    print(f"关注者总数 {data.get('total', 0)}，本次返回 {len(followers)} 个")
    return list(followers)


async def list_followers(api: WeChatAPI) -> None:
    for openid in await fetch_followers(api):
        print(f"  {openid}   (脱敏: {mask_id(openid)})")


async def main() -> int:
    _utf8()
    parser = argparse.ArgumentParser(description="主动发送客服消息")
    parser.add_argument("--openid", default="", help="目标用户 openid")
    parser.add_argument("--text", default="", help="消息内容（支持 emoji）")
    parser.add_argument("--list", action="store_true", help="只列出关注者 openid")
    parser.add_argument("--all", action="store_true", help="给所有关注者逐条发送")
    args = parser.parse_args()

    api = WeChatAPI(settings.wechat_appid, settings.wechat_appsecret)
    try:
        if args.all:
            if not args.text:
                print("群发需要同时给出 --text")
                return 1
            followers = await fetch_followers(api)
            if not followers:
                print("没有可发送的对象。")
                return 1
            ok = fail = 0
            for index, openid in enumerate(followers, start=1):
                try:
                    await api.send_text(openid, args.text)
                    ok += 1
                    print(f"[{index}/{len(followers)}] [OK]   {mask_id(openid)}")
                except WeChatAPIError as exc:
                    fail += 1
                    hint = ""
                    if exc.errcode == 45015:
                        hint = "（该用户 48 小时内没互动，微信不允许推送）"
                    elif exc.errcode == 40003:
                        hint = "（openid 无效）"
                    print(f"[{index}/{len(followers)}] [FAIL] {mask_id(openid)} errcode={exc.errcode} {hint}")
                await asyncio.sleep(0.6)  # 轻微间隔，降低触发频控概率
            print(f"\n完成：成功 {ok}，失败 {fail}，共 {len(followers)} 人")
            print(f"内容：{args.text}")
            return 0 if ok else 1

        if args.list or not args.openid or not args.text:
            if not args.list:
                print("没有指定 --openid 和 --text，下面只列出关注者：\n")
            await list_followers(api)
            return 0

        await api.send_text(args.openid, args.text)
        print(f"发送成功 -> {mask_id(args.openid)}")
        print(f"内容：{args.text}")
        return 0
    except WeChatAPIError as exc:
        print(f"发送失败：{exc}")
        if exc.errcode == 45015:
            print("原因：该用户 48 小时内没有和公众号互动，微信不允许主动推送。")
        elif exc.errcode == 40003:
            print("原因：openid 不对（注意要用关注者列表里的完整 openid）。")
        return 1
    finally:
        await api.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
