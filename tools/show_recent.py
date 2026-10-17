#!/usr/bin/env python
"""查看线上真实对话记录（默认脱敏 openid），用来确认公众号里到底发生了什么。

用法：
    .\\.venv\\Scripts\\python.exe tools\\show_recent.py            # 最近 10 条
    .\\.venv\\Scripts\\python.exe tools\\show_recent.py -n 30
    .\\.venv\\Scripts\\python.exe tools\\show_recent.py --openid on6wW3... --full
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.textutil import mask_id  # noqa: E402


def _utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main() -> int:
    _utf8()
    parser = argparse.ArgumentParser(description="查看最近的对话记录")
    parser.add_argument("-n", type=int, default=10, help="显示最近多少条（默认 10）")
    parser.add_argument("--openid", default="", help="只看某个用户")
    parser.add_argument("--full", action="store_true", help="不脱敏 openid")
    parser.add_argument("--users", action="store_true", help="只列出用户与其消息数")
    args = parser.parse_args()

    if not settings.db_path.is_file():
        print(f"没有找到会话库：{settings.db_path}")
        return 1

    conn = sqlite3.connect(str(settings.db_path))
    try:
        if args.users:
            rows = conn.execute(
                "SELECT openid, COUNT(*), MAX(ts) FROM messages GROUP BY openid ORDER BY MAX(ts) DESC"
            ).fetchall()
            print(f"共 {len(rows)} 个用户（按最近活跃排序）：")
            for openid, count, ts in rows:
                stamp = time.strftime("%m-%d %H:%M:%S", time.localtime(ts))
                print(f"  {openid if args.full else mask_id(openid):<20} 消息 {count:>4} 条   最近 {stamp}")
            return 0

        query = "SELECT openid, role, content, ts FROM messages"
        params: list = []
        if args.openid:
            query += " WHERE openid LIKE ?"
            params.append(f"{args.openid}%")
        query += " ORDER BY id DESC LIMIT ?"
        params.append(args.n)

        rows = list(reversed(conn.execute(query, params).fetchall()))
        if not rows:
            print("还没有任何对话记录。")
            return 0
        print(f"最近 {len(rows)} 条（时间 角色 openid: 内容）\n")
        for openid, role, content, ts in rows:
            who = "用户" if role == "user" else "AI  "
            stamp = time.strftime("%m-%d %H:%M:%S", time.localtime(ts))
            name = openid if args.full else mask_id(openid)
            print(f"[{stamp}] {who} {name}:")
            print(f"    {content}\n")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
