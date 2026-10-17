"""会话记忆、消息去重、频率限制。

- SessionStore：按 openid 保存最近若干轮对话，落 SQLite（重启不丢），带 TTL；
- SeenCache：内存级 MsgId 去重，防止微信超时重试导致重复调用 DeepSeek；
- RateLimiter：单用户最小间隔 + 每小时上限，防止被刷爆 API 额度。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)


class SessionStore:
    def __init__(self, db_path: Path, history_turns: int, ttl: int) -> None:
        self.history_turns = max(0, history_turns)
        self.ttl = max(0, ttl)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                openid  TEXT    NOT NULL,
                role    TEXT    NOT NULL,
                content TEXT    NOT NULL,
                ts      REAL    NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_openid ON messages(openid, id)"
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def history(self, openid: str) -> list[dict[str, str]]:
        """返回最近 history_turns 轮（user+assistant 成对）的消息。"""
        if self.history_turns <= 0:
            return []
        limit = self.history_turns * 2
        cutoff = time.time() - self.ttl if self.ttl else 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content FROM messages WHERE openid=? AND ts>=? "
                "ORDER BY id DESC LIMIT ?",
                (openid, cutoff, limit),
            ).fetchall()
        return [{"role": role, "content": content} for role, content in reversed(rows)]

    def append(self, openid: str, role: str, content: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages(openid, role, content, ts) VALUES(?,?,?,?)",
                (openid, role, content, time.time()),
            )
            self._conn.commit()

    def clear(self, openid: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE openid=?", (openid,))
            self._conn.commit()

    def count_users(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(DISTINCT openid) FROM messages").fetchone()
        return int(row[0]) if row else 0

    def purge_expired(self) -> int:
        if not self.ttl:
            return 0
        cutoff = time.time() - self.ttl * 4  # 保留 4 倍 TTL 作为存档
        with self._lock:
            cur = self._conn.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))
            self._conn.commit()
        return cur.rowcount or 0


class SeenCache:
    """带 TTL 的内存去重集合（微信同一 MsgId 最多重试 3 次）。"""

    def __init__(self, ttl: int = 300, max_items: int = 5000) -> None:
        self.ttl = ttl
        self.max_items = max_items
        self._items: dict[str, float] = {}
        self._lock = threading.Lock()

    def mark(self, key: str) -> bool:
        """首次出现返回 True，重复出现返回 False。"""
        if not key:
            return True
        now = time.time()
        with self._lock:
            self._gc(now)
            if key in self._items:
                return False
            self._items[key] = now
            return True

    def _gc(self, now: float) -> None:
        if len(self._items) > self.max_items:
            self._items = {k: v for k, v in self._items.items() if now - v < self.ttl}
        expired = [k for k, v in self._items.items() if now - v >= self.ttl]
        for key in expired:
            self._items.pop(key, None)


class RateLimiter:
    """每个用户：最小间隔 + 每小时条数上限。"""

    def __init__(self, min_interval: float, per_hour: int) -> None:
        self.min_interval = max(0.0, min_interval)
        self.per_hour = max(1, per_hour)
        self._last: dict[str, float] = {}
        self._window: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, str]:
        now = time.time()
        with self._lock:
            last = self._last.get(key, 0.0)
            if self.min_interval and now - last < self.min_interval:
                return False, "too_fast"
            window = self._window.setdefault(key, deque())
            while window and now - window[0] > 3600:
                window.popleft()
            if len(window) >= self.per_hour:
                return False, "hourly_limit"
            window.append(now)
            self._last[key] = now
        return True, ""
