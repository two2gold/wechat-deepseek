#!/usr/bin/env python
"""实时查看用户发来的消息（带时间戳）。

三种用法：
    python tools\\tail_messages.py             # 先显示最近 8 条，然后持续跟随新消息
    python tools\\tail_messages.py --last 20   # 先显示最近 20 条再跟随
    python tools\\tail_messages.py --once --last 20   # 只看历史，不跟随

它会同时把内容追加写到 logs\\messages.log（UTF-8），
万一终端把中文显示乱了，用记事本打开这个文件看即可。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.textutil import mask_id  # noqa: E402

CONTENT_WIDTH = 76  # 按"显示宽度"折行（中文算 2 列）


def _setup_output() -> None:
    # 只有在输出被重定向时才改成 UTF-8；直接写控制台时保持默认，
    # 让 Python 走 WriteConsoleW（中文才不会变乱码）。
    if not sys.stdout.isatty():
        reconfigure = getattr(sys.stdout, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _disp_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def wrap_display(text: str, width: int = CONTENT_WIDTH) -> list[str]:
    """按终端显示宽度折行（中文按 2 列算），避免超宽被终端硬折。"""
    lines: list[str] = []
    current = ""
    used = 0
    for ch in text:
        char_width = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used + char_width > width and current:
            lines.append(current)
            current, used = "", 0
        current += ch
        used += char_width
    if current:
        lines.append(current)
    return lines or [""]


def format_row(openid: str, role: str, content: str, ts: float) -> list[str]:
    stamp = time.strftime("%H:%M:%S", time.localtime(ts))
    text = " ".join((content or "").split())  # 折叠换行，避免一次刷很多行
    if role == "user":
        head = f"[{stamp}] 收到 {mask_id(openid)}: "
        indent = " " * 9
    else:
        head = f"[{stamp}] 回复: "
        indent = " " * 9
    wrapped = wrap_display(text, CONTENT_WIDTH - _disp_width(head))
    out = [head + wrapped[0]]
    out.extend(indent + line for line in wrapped[1:])
    return out


def main() -> int:
    _setup_output()
    parser = argparse.ArgumentParser(description="实时查看用户消息")
    parser.add_argument("--last", type=int, default=8, help="启动时先显示最近多少条（默认 8）")
    parser.add_argument("--interval", type=float, default=2.0, help="轮询间隔秒（默认 2）")
    parser.add_argument("--once", action="store_true", help="只显示历史后退出，不持续跟随")
    parser.add_argument("--full", action="store_true", help="openid 不脱敏")
    args = parser.parse_args()

    log_path = settings.db_path.parent / "messages.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(lines: list[str], mirror: bool = True) -> None:
        for line in lines:
            print(line, flush=True)
        if mirror:
            try:
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write("\n".join(lines) + "\n")
            except OSError:
                pass

    def connect() -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    # 等数据库出现（服务刚启动时可能还没建库）
    waited = 0
    while not settings.db_path.is_file():
        if waited == 0:
            print("等待会话数据库出现 ...", flush=True)
        time.sleep(1)
        waited += 1
        if waited > 30:
            print(f"等不到数据库：{settings.db_path}", flush=True)
            return 1

    conn = connect()
    last_id = 0
    if args.last > 0:
        rows = conn.execute(
            "SELECT id, openid, role, content, ts FROM messages ORDER BY id DESC LIMIT ?",
            (args.last,),
        ).fetchall()
        if rows:
            print(f"---- 最近 {len(rows)} 条 ----", flush=True)
            for row in reversed(rows):
                emit(format_row(row["openid"], row["role"], row["content"], row["ts"]), mirror=False)
            print("---- 以下为实时消息 ----", flush=True)
        last_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]

    if args.once:
        conn.close()
        return 0

    while True:
        time.sleep(args.interval)
        try:
            rows = conn.execute(
                "SELECT id, openid, role, content, ts FROM messages WHERE id > ? ORDER BY id LIMIT 100",
                (last_id,),
            ).fetchall()
        except sqlite3.Error:
            # 数据库被换掉或损坏时重连一次
            try:
                conn.close()
            except sqlite3.Error:
                pass
            time.sleep(1)
            conn = connect()
            continue
        for row in rows:
            last_id = max(last_id, row["id"])
            emit(format_row(row["openid"], row["role"], row["content"], row["ts"]))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        raise SystemExit(0)
