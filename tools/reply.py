#!/usr/bin/env python
"""人工主动回复控制台：看到用户消息后，以公众号身份手动回一句。

用法（双击 reply.cmd 即可，或直接跑本脚本）：
    python tools\\reply.py                  # 列出最近给你发过消息的人，选一个直接回
    python tools\\reply.py --openid on6wW3...   # 直接指定收件人
    python tools\\reply.py --all            # 给所有关注者群发（会二次确认）

交互命令：
    /to N      切换收件人（用列表里的编号）
    /users     重新列出最近发过消息的人
    /followers 列出所有关注者（需要账号有"获取用户列表"权限）
    /all       给所有关注者群发当前这条内容
    /full      切换 openid 是否脱敏显示
    /quit      退出

说明：
- 客服消息有 48 小时窗口：对方超过 48 小时没和公众号互动，微信会拒收（errcode 45015）。
- 你手动发出的内容会同时写入会话库（记成 assistant），这样 AI 后续回复能"记得"你说过的话。
  不想要这个行为就加 --no-record。
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402
from app.textutil import mask_id, truncate_bytes  # noqa: E402
from app.wechat.api import WeChatAPI, WeChatAPIError  # noqa: E402

WINDOW_HOURS = 48


def _setup_output() -> None:
    if not sys.stdout.isatty():
        reconfigure = getattr(sys.stdout, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def recent_senders(limit: int = 10) -> list[dict]:
    """最近给我发过消息的人（按最后一条消息时间倒序）。"""
    if not settings.db_path.is_file():
        return []
    conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """
            SELECT openid,
                   MAX(ts) AS last_ts,
                   (SELECT content FROM messages m2
                     WHERE m2.openid = m1.openid AND m2.role = 'user'
                     ORDER BY m2.id DESC LIMIT 1) AS last_user_msg
              FROM messages m1
             WHERE role = 'user'
             GROUP BY openid
             ORDER BY last_ts DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [{"openid": r[0], "ts": r[1], "text": r[2] or ""} for r in rows]


def record_reply(openid: str, text: str) -> None:
    """把你手动发的消息写进会话记录（AI 之后能看到自己"说过"这句）。"""
    if not settings.db_path.is_file():
        return
    try:
        conn = sqlite3.connect(str(settings.db_path))
        conn.execute(
            "INSERT INTO messages(openid, role, content, ts) VALUES(?,?,?,?)",
            (openid, "assistant", text, time.time()),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        print(f"（提示：写入会话记录失败：{exc}）")


async def fetch_followers() -> list[str]:
    api = WeChatAPI(settings.wechat_appid, settings.wechat_appsecret)
    try:
        token = await api.access_token()
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://api.weixin.qq.com/cgi-bin/user/get", params={"access_token": token}
            )
        data = resp.json()
        if data.get("errcode", 0) != 0:
            print(f"取关者列表失败：errcode={data.get('errcode')} {data.get('errmsg')}")
            print("（测试号/未认证号可能没有该接口权限，不影响单发）")
            return []
        return list((data.get("data") or {}).get("openid") or [])
    finally:
        await api.aclose()


async def send_one(openid: str, text: str, dry_run: bool = False) -> bool:
    if dry_run:
        print(f"  (dry-run) 本应发送给 {mask_id(openid)}：{text[:60]}")
        return True
    api = WeChatAPI(settings.wechat_appid, settings.wechat_appsecret)
    try:
        await api.send_text(openid, text)
        return True
    except WeChatAPIError as exc:
        if exc.errcode == 45015:
            print("  ✗ 发送失败：对方超过 48 小时没和公众号互动，微信不允许主动推送（45015）")
            print("    等对方再发一条消息即可恢复。")
        elif exc.errcode == 48001:
            print(f"  ✗ 发送失败：账号没有客服消息接口权限（48001）")
        elif exc.errcode == 40003:
            print(f"  ✗ 发送失败：openid 不正确（40003）")
        else:
            print(f"  ✗ 发送失败：{exc}")
        return False
    finally:
        await api.aclose()


def print_senders(senders: list[dict], full: bool, current: int | None) -> None:
    if not senders:
        print("会话库里还没有任何人给你发过消息。")
        return
    now = time.time()
    print("最近给你发过消息的人：")
    for index, item in enumerate(senders, start=1):
        name = item["openid"] if full else mask_id(item["openid"])
        stamp = time.strftime("%m-%d %H:%M", time.localtime(item["ts"]))
        age_h = (now - item["ts"]) / 3600
        flag = "  " if age_h <= WINDOW_HOURS else "⚠ "
        note = "" if age_h <= WINDOW_HOURS else f"（{age_h:.0f} 小时没互动，可能发不出去）"
        mark = "*" if current == index else " "
        snippet = " ".join(item["text"].split())[:40]
        print(f" {mark}{index}) {flag}{name:<22} 最后 {stamp}  「{snippet}」{note}")


def read_line(prompt: str) -> str:
    """读一行输入并清理：去掉 BOM/零宽字符（管道或复制粘贴常带），再去首尾空白。

    这一层很重要 —— 曾经因为输入里带 BOM 导致命令行被当成正文发出去。
    """
    raw = input(prompt)
    for junk in ("\ufeff", "\u200b", "\u200e", "\u200f"):
        raw = raw.replace(junk, "")
    return raw.strip()


KNOWN_COMMANDS = ("/users", "/followers", "/full", "/all", "/quit", "/exit", "/q")


def main() -> int:
    _setup_output()
    parser = argparse.ArgumentParser(description="人工主动回复控制台")
    parser.add_argument("--openid", default="", help="直接指定收件人，跳过选择")
    parser.add_argument("--all", action="store_true", help="给所有关注者群发")
    parser.add_argument("--no-record", action="store_true", help="不把手动回复写进会话记录")
    parser.add_argument("--dry-run", action="store_true", help="只演示，不真的发送")
    parser.add_argument("--limit", type=int, default=10, help="列表显示多少人（默认 10）")
    args = parser.parse_args()

    print("=" * 58)
    print(" 人工主动回复控制台   (/quit 退出，空行取消)")
    print("=" * 58)

    senders = recent_senders(args.limit)
    full = False
    current_index: int | None = None
    target = args.openid

    if target:
        print(f"收件人已指定：{mask_id(target)}")
    else:
        print_senders(senders, full, None)
        if senders:
            current_index = 1
            target = senders[0]["openid"]
            print(f"\n默认回复最近发消息的那个人（编号 1）。用 /to N 切换。")

    if args.all:
        followers = asyncio.run(fetch_followers())
        if not followers:
            print("没有拿到关注者列表，无法群发。")
            return 1
        text = read_line("要群发的内容：")
        if not text:
            print("已取消。")
            return 0
        confirm = read_line(f"确认发给全部 {len(followers)} 人？输入 yes 继续：").lower()
        if confirm != "yes":
            print("已取消。")
            return 0
        ok = 0
        for openid in followers:
            if asyncio.run(send_one(openid, text, args.dry_run)):
                ok += 1
                print(f"  ✓ {mask_id(openid)}")
        print(f"完成：成功 {ok}/{len(followers)}")
        return 0

    while True:
        try:
            line = read_line("\n消息> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not line:
            print("（已取消本条）")
            continue
        # 以 / 开头但不是已知命令：不发送，避免误把命令当正文发出去。
        # 确实想发以 / 开头的内容，就用 // 前缀（会被还原成单个 /）。
        if line.startswith("//"):
            line = line[1:]
        elif line.startswith("/") and line not in KNOWN_COMMANDS and not line.startswith("/to "):
            print(f"未知命令：{line}")
            print("（不发送。若确实要发送以 / 开头的内容，请写成 //开头）")
            continue
        if line in ("/quit", "/exit", "/q"):
            return 0
        if line == "/users":
            senders = recent_senders(args.limit)
            print_senders(senders, full, current_index)
            continue
        if line == "/followers":
            followers = asyncio.run(fetch_followers())
            for index, openid in enumerate(followers, start=1):
                print(f"  {index}) {openid if full else mask_id(openid)}")
            continue
        if line == "/full":
            full = not full
            print(f"openid 显示：{'完整' if full else '脱敏'}")
            print_senders(senders, full, current_index)
            continue
        if line.startswith("/to "):
            try:
                index = int(line.split()[1])
            except (IndexError, ValueError):
                print("用法：/to 2")
                continue
            if not senders:
                senders = recent_senders(args.limit)
            if 1 <= index <= len(senders):
                current_index = index
                target = senders[index - 1]["openid"]
                print(f"收件人切换为 {mask_id(target)}")
            else:
                print(f"编号超出范围（1-{len(senders)}）")
            continue
        if line == "/all":
            followers = asyncio.run(fetch_followers())
            if not followers:
                print("没有拿到关注者列表。")
                continue
            text = read_line(f"要群发的内容（发给 {len(followers)} 人）：")
            if not text:
                print("已取消。")
                continue
            if read_line("确认？输入 yes 继续：").lower() != "yes":
                print("已取消。")
                continue
            ok = 0
            for openid in followers:
                if asyncio.run(send_one(openid, text, args.dry_run)):
                    ok += 1
            print(f"完成：成功 {ok}/{len(followers)}")
            continue

        if not target:
            print("还没有收件人：先用 /to N 选一个，或用 --openid 指定。")
            continue

        payload = truncate_bytes(line, settings.max_reply_bytes)
        print(f"发送给 {mask_id(target)} ...")
        if asyncio.run(send_one(target, payload, args.dry_run)):
            print("  ✓ 已发送（对方微信里会收到）")
            if not args.no_record:
                record_reply(target, payload)


if __name__ == "__main__":
    raise SystemExit(main())
