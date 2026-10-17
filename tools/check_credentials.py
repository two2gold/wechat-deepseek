#!/usr/bin/env python
"""凭据体检：验证 AppID/AppSecret、IP 白名单、客服消息通道、DeepSeek Key 是否真的可用。

用法（在项目根目录）：
    .\\.venv\\Scripts\\python.exe tools\\check_credentials.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402
from app.deepseek import DeepSeekClient, DeepSeekError  # noqa: E402
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


def head(title: str) -> None:
    print(f"\n==== {title} ====")


async def check_wechat() -> str:
    """返回可用作推送测试的 openid（没有则返回空串）。"""
    head("1. 微信 AppID / AppSecret / IP 白名单")
    print(f"AppID   : {settings.wechat_appid}")
    print(f"AppSecret: {'已配置(长度 %d)' % len(settings.wechat_appsecret) if settings.wechat_appsecret else '未配置'}")

    api = WeChatAPI(settings.wechat_appid, settings.wechat_appsecret)
    try:
        token = await api.access_token(force=True)
        print(f"[OK] access_token 获取成功，长度 {len(token)}（有效期约 2 小时，已缓存复用）")
    except WeChatAPIError as exc:
        print(f"[FAIL] {exc}")
        if exc.errcode == 40164:
            print("       → 到公众号后台「设置与开发 - 基本配置 - IP 白名单」加入本机公网 IP")
        elif exc.errcode == 40013:
            print("       → AppID 不正确")
        elif exc.errcode == 40125:
            print("       → AppSecret 不正确，请到后台重置后重新填写")
        await api.aclose()
        return ""

    openid = ""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            "https://api.weixin.qq.com/cgi-bin/user/get",
            params={"access_token": token},
        )
        data = resp.json()
    if data.get("errcode", 0) == 0:
        followers = (data.get("data") or {}).get("openid") or []
        print(f"[OK] 关注者总数 {data.get('total', 0)}，本次取到 {len(followers)} 个 openid")
        if followers:
            openid = followers[0]
            print(f"     取第一个用于推送测试：{mask_id(openid)}")
    else:
        print(f"[提示] 无法获取关注者列表：errcode={data.get('errcode')} {data.get('errmsg')}")
        print("       测试号/未认证号没有该接口权限，这很正常，不影响客服消息推送")

    head("2. 客服消息通道")
    target = openid or "oInvalidOpenidForChannelTest"
    note = "（真实关注者，会真的收到一条微信）" if openid else "（不存在的 openid，只看接口能否调通）"
    print(f"测试目标：{mask_id(target)} {note}")
    try:
        await api.send_text(target, "✅ 客服消息通道测试：DeepSeek 公众号助手已就绪。")
        print("[OK] 客服消息发送成功 —— 公众号会话里应该已经收到这条消息")
    except WeChatAPIError as exc:
        if exc.errcode == 40003:
            print("[OK] 接口调通了（返回 40003 invalid openid，说明 token 与权限都正常）")
        elif exc.errcode == 48001:
            print(f"[FAIL] 账号没有客服消息接口权限：{exc}")
        elif exc.errcode == 45015:
            print("[提示] 45015 = 该用户超过 48 小时没和公众号互动，微信不允许主动推送。")
            print("       这不是配置错误：真实对话里用户刚发完消息，窗口是打开的，推送正常。")
            print("       想现场验证：用微信给公众号发任意一句话，然后立刻重跑本脚本。")
        else:
            print(f"[FAIL] {exc}")

    await api.aclose()
    return openid


async def check_deepseek() -> None:
    head("3. DeepSeek API Key")
    client = DeepSeekClient(
        settings.deepseek_api_key,
        settings.deepseek_base_url,
        settings.deepseek_model,
        temperature=0.7,
        max_tokens=120,
        timeout=60.0,
        system_prompt="你是一个简洁的助手，回答不超过 50 字。",
        mock=settings.deepseek_mock,
    )
    if settings.deepseek_mock:
        print("当前是 DEEPSEEK_MOCK=1，跳过真实调用（先关掉模拟模式再体检）")
        await client.aclose()
        return
    messages = client.build_messages([], "用一句话说明你是哪个模型。")
    started = time.time()
    try:
        reply = await client.chat(messages)
        print(f"[OK] 调用成功，耗时 {time.time() - started:.2f}s")
        print(f"     回复：{reply[:120]}")
    except DeepSeekError as exc:
        print(f"[FAIL] {exc}")
    finally:
        await client.aclose()


async def main() -> int:
    _utf8()
    head("0. 配置概览")
    print(f"模型     : {settings.deepseek_model}")
    print(f"回复策略 : {settings.reply_mode}（被动等待 {settings.passive_timeout}s）")
    print(f"模拟模式 : {settings.deepseek_mock}")
    for warning in settings.validate():
        print(f"[提醒] {warning}")

    await check_wechat()
    await check_deepseek()

    head("4. 公众号后台该填什么")
    url = ""
    url_file = Path(__file__).resolve().parent.parent / "logs" / "tunnel.url"
    if url_file.is_file():
        url = url_file.read_text(encoding="utf-8-sig").strip()
    print(f"URL        : {url + '/wechat' if url else '（未启动隧道，用你的域名）'}")
    print(f"Token      : {settings.wechat_token}")
    print(f"EncodingAESKey: {settings.wechat_aes_key or '（未配置，走明文/兼容模式）'}")
    print("消息加解密方式: 明文模式（推荐先跑通）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
