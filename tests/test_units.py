"""单元测试：加解密、签名、文本工具、会话存储、限流。"""

from __future__ import annotations

import base64
import time

import pytest
from conftest import AES_KEY, APPID, TOKEN

from app.memory import RateLimiter, SeenCache, SessionStore
from app.textutil import normalize_input, split_by_bytes, strip_markdown, truncate_bytes
from app.wechat.crypto import WeChatCrypto, WeChatCryptoError
from app.wechat.xml import WeChatXMLError, parse_message, text_reply_xml


# ------------------------------------------------------------------ 签名
def test_signature_matches_manual_sha1():
    import hashlib

    crypto = WeChatCrypto(TOKEN)
    ts, nonce = "1700000000", "abc123"
    expected = hashlib.sha1("".join(sorted([TOKEN, ts, nonce])).encode()).hexdigest()
    assert crypto.sign(ts, nonce) == expected
    assert crypto.check_signature(expected, ts, nonce) is True
    assert crypto.check_signature("bad", ts, nonce) is False


def test_safe_mode_roundtrip():
    crypto = WeChatCrypto(TOKEN, AES_KEY, APPID)
    assert crypto.safe_mode_enabled is True
    xml = text_reply_xml("oUser", "gh_x", "中文 + emoji 🎉 & <tag>")
    assert crypto.decrypt(crypto.encrypt(xml)) == xml


def test_decrypt_with_wrong_appid_is_rejected():
    enc = WeChatCrypto(TOKEN, AES_KEY, "wx_other_appid").encrypt("<xml>hi</xml>")
    with pytest.raises(WeChatCryptoError):
        WeChatCrypto(TOKEN, AES_KEY, APPID).decrypt(enc)


def test_decrypt_with_wrong_key_fails():
    enc = WeChatCrypto(TOKEN, AES_KEY, APPID).encrypt("<xml>hi</xml>")
    other_key = base64.urlsafe_b64encode(bytes(range(1, 33))).decode().rstrip("=")
    with pytest.raises(WeChatCryptoError):
        WeChatCrypto(TOKEN, other_key, APPID).decrypt(enc)


def test_invalid_aes_key_length():
    with pytest.raises(WeChatCryptoError):
        WeChatCrypto(TOKEN, "too-short", APPID)


def test_plain_mode_cannot_encrypt():
    with pytest.raises(WeChatCryptoError):
        WeChatCrypto(TOKEN).encrypt("<xml/>")


# ------------------------------------------------------------------ XML
def test_parse_and_build_xml():
    xml = text_reply_xml("oUser", "gh_x", "带 ]]> 的文本")
    data = parse_message(xml)
    assert data["ToUserName"] == "oUser"
    assert data["Content"] == "带 ]]> 的文本"
    assert data["MsgType"] == "text"
    assert int(data["CreateTime"]) > 0


def test_broken_xml_raises():
    with pytest.raises(WeChatXMLError):
        parse_message("<xml><ToUserName>")


# ------------------------------------------------------------------ 文本工具
def test_truncate_bytes_keeps_utf8_intact():
    text = "中" * 1000  # 3000 字节
    out = truncate_bytes(text, 100)
    assert len(out.encode("utf-8")) <= 100
    assert "截断" in out
    assert truncate_bytes("短文本", 100) == "短文本"


def test_split_by_bytes():
    text = "。".join(["段落" * 20] * 10)
    chunks = split_by_bytes(text, 200)
    assert len(chunks) > 1
    assert all(len(c.encode("utf-8")) <= 200 for c in chunks)
    # 切分不应丢内容（切块时会去掉块首尾空白）
    joined = "".join(chunks)
    assert joined.replace("\n", "") == text.replace("\n", "")


def test_strip_markdown():
    raw = "# 标题\n\n**加粗** 与 `代码`\n\n```python\nprint(1)\n```\n\n[链接](http://a.com)"
    out = strip_markdown(raw)
    assert "#" not in out
    assert "**" not in out
    assert "`" not in out
    assert "print(1)" in out
    assert "链接" in out and "http" not in out


def test_strip_markdown_keeps_code_identifiers():
    """回归测试：下划线曾把代码标识符吃掉（read_csv -> readcsv）。"""
    raw = (
        "用 pandas 三行就够：\n\n"
        "```python\nimport pandas as pd\ndf = pd.read_csv('a.csv')\nprint(df.head())\n```\n\n"
        "补充：`pd.set_option('display.max_columns', None)`，"
        "类里的 __init__ 和 snake_case_name、a*b*c 都不要被改。"
    )
    out = strip_markdown(raw)
    for token in ("read_csv", "set_option", "__init__", "snake_case_name", "a*b*c", "head()"):
        assert token in out, f"{token} 被误伤：{out}"
    assert "```" not in out and "`" not in out


def test_strip_markdown_italic_still_works():
    assert strip_markdown("这是 *重点* 内容") == "这是 重点 内容"
    assert strip_markdown("这是 _重点_ 内容") == "这是 重点 内容"


def test_normalize_input():
    assert normalize_input("  hi\u200b  ", 10) == "hi"
    assert len(normalize_input("a" * 100, 10)) == 10


# ------------------------------------------------------------------ 存储与限流
def test_session_store_history_and_clear(tmp_path):
    store = SessionStore(tmp_path / "s.db", history_turns=2, ttl=3600)
    for i in range(3):
        store.append("u1", "user", f"q{i}")
        store.append("u1", "assistant", f"a{i}")
    history = store.history("u1")
    assert len(history) == 4  # 只留最近 2 轮
    assert history[0]["content"] == "q1"
    assert store.count_users() == 1
    store.clear("u1")
    assert store.history("u1") == []
    store.close()


def test_session_store_ttl(tmp_path):
    store = SessionStore(tmp_path / "s.db", history_turns=5, ttl=1)
    store.append("u1", "user", "old")
    time.sleep(1.1)
    assert store.history("u1") == []
    store.close()


def test_seen_cache():
    cache = SeenCache(ttl=60)
    assert cache.mark("a") is True
    assert cache.mark("a") is False
    assert cache.mark("b") is True


def test_rate_limiter():
    limiter = RateLimiter(min_interval=0.5, per_hour=2)
    assert limiter.allow("u")[0] is True
    allowed, reason = limiter.allow("u")
    assert allowed is False and reason == "too_fast"
    limiter.min_interval = 0.0
    assert limiter.allow("u")[0] is True
    allowed, reason = limiter.allow("u")
    assert allowed is False and reason == "hourly_limit"
