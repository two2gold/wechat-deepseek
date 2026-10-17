#!/usr/bin/env python
"""模拟微信服务器，用来联调自建服务（不需要真的公众号也能测）。

用法：

  # 1) 本地明文模式（先开 DEEPSEEK_MOCK=1 跑通链路）
  python tools/fake_wechat.py --url http://127.0.0.1:8000/wechat --token change-me-please

  # 2) 安全模式
  python tools/fake_wechat.py --url http://127.0.0.1:8000/wechat --token xxx ^
      --aes-key <43位EncodingAESKey> --appid wxYOUR_APPID

  # 3) 只测 URL 校验（对应后台点"提交"那一步）
  python tools/fake_wechat.py --url https://你的域名/wechat --token xxx --check-only

  # 4) 交互式连续对话（验证多轮记忆）
  python tools/fake_wechat.py --url http://127.0.0.1:8000/wechat --token xxx
"""

from __future__ import annotations

import argparse
import random
import string
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.wechat.crypto import WeChatCrypto  # noqa: E402
from app.wechat.xml import WeChatXMLError, parse_message  # noqa: E402


def _make_output_utf8() -> None:
    """Windows 控制台默认 GBK，中文/符号容易直接抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def rand_nonce(length: int = 10) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))


def rand_msg_id() -> str:
    return str(random.randint(10**16, 10**17 - 1))


def build_text_xml(to_user: str, from_user: str, content: str, msg_id: str) -> str:
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        f"<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>"
        f"<MsgId>{msg_id}</MsgId>"
        "</xml>"
    )


def verify_url(client: httpx.Client, url: str, crypto: WeChatCrypto) -> bool:
    """模拟后台点击"提交"时的 URL 校验。"""
    timestamp, nonce, echostr = str(int(time.time())), rand_nonce(), rand_nonce(16)
    resp = client.get(
        url,
        params={
            "signature": crypto.sign(timestamp, nonce),
            "timestamp": timestamp,
            "nonce": nonce,
            "echostr": echostr,
        },
    )
    ok = resp.status_code == 200 and resp.text.strip() == echostr
    flag = "[OK] 通过" if ok else "[FAIL] 失败"
    print(f"[URL 校验] status={resp.status_code} 返回={resp.text.strip()!r} => {flag}")
    if not ok:
        print("   排查：Token 是否与后台一致；URL 是否完全一致（含路径）；服务能否被公网访问")
    return ok


def send_text(
    client: httpx.Client,
    url: str,
    crypto: WeChatCrypto,
    *,
    openid: str,
    gh_id: str,
    text: str,
    msg_id: str,
) -> None:
    """模拟用户发一条文本消息，并解析服务端的被动回复。"""
    inner = build_text_xml(gh_id, openid, text, msg_id)
    timestamp, nonce = str(int(time.time())), rand_nonce()
    params: dict[str, str] = {
        "signature": crypto.sign(timestamp, nonce),
        "timestamp": timestamp,
        "nonce": nonce,
        "openid": openid,
    }

    if crypto.safe_mode_enabled:
        encrypt = crypto.encrypt(inner)
        params["encrypt_type"] = "aes"
        params["msg_signature"] = crypto.msg_signature(timestamp, nonce, encrypt)
        body = (
            f"<xml><ToUserName><![CDATA[{gh_id}]]></ToUserName>"
            f"<Encrypt><![CDATA[{encrypt}]]></Encrypt></xml>"
        )
    else:
        body = inner

    started = time.time()
    resp = client.post(
        url,
        params=params,
        content=body.encode("utf-8"),
        headers={"Content-Type": "text/xml"},
    )
    elapsed = time.time() - started
    print(f"[发送] 「{text}」 => status={resp.status_code} 耗时={elapsed:.2f}s")

    payload = resp.text.strip()
    if payload in ("", "success"):
        print("[回复] 无被动回复（服务端走客服消息异步推送，请到公众号会话里查看）")
        return
    if not payload.startswith("<"):
        print(f"[回复] 服务端返回了非 XML 内容：{payload[:200]}")
        print("       常见原因：隧道已断开（no tunnel / 502）、Nginx 报错、或 URL 写错")
        return

    try:
        outer = parse_message(payload)
        xml_text = crypto.decrypt(outer["Encrypt"]) if "Encrypt" in outer else payload
        reply = parse_message(xml_text)
    except (WeChatXMLError, KeyError, ValueError) as exc:
        print(f"[回复] 解析失败：{exc}\n原始内容：{payload[:400]}")
        return
    print(f"[回复] MsgType={reply.get('MsgType')} Content={reply.get('Content', '')!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description="模拟微信服务器联调工具")
    parser.add_argument("--url", default="http://127.0.0.1:8000/wechat", help="你的服务地址（含路径）")
    parser.add_argument("--token", required=True, help="与后台一致的 Token")
    parser.add_argument("--aes-key", default="", help="EncodingAESKey，安全模式才需要")
    parser.add_argument("--appid", default="", help="公众号 AppID，安全模式用于校验密文")
    parser.add_argument("--text", default="", help="要发送的消息；留空则进入交互模式")
    parser.add_argument("--openid", default="oTestUser0001", help="模拟的用户 openid")
    parser.add_argument("--gh-id", default="gh_test_account", help="模拟的公众号原始ID")
    parser.add_argument("--check-only", action="store_true", help="只做 URL 校验")
    args = parser.parse_args()
    _make_output_utf8()

    crypto = WeChatCrypto(args.token, args.aes_key, args.appid)
    print(f"目标：{args.url}")
    print(f"模式：{'安全模式（AES）' if crypto.safe_mode_enabled else '明文/兼容模式'}")

    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        ok = verify_url(client, args.url, crypto)
        if args.check_only:
            return 0 if ok else 1
        if not ok:
            print("URL 校验未通过，后续消息测试大概率也会失败，仍继续尝试……")

        if args.text:
            send_text(
                client,
                args.url,
                crypto,
                openid=args.openid,
                gh_id=args.gh_id,
                text=args.text,
                msg_id=rand_msg_id(),
            )
            return 0

        print("\n进入交互模式，输入内容回车发送（Ctrl+C 退出）：")
        while True:
            try:
                text = input("你 > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not text:
                continue
            send_text(
                client,
                args.url,
                crypto,
                openid=args.openid,
                gh_id=args.gh_id,
                text=text,
                msg_id=rand_msg_id(),
            )


if __name__ == "__main__":
    raise SystemExit(main())
