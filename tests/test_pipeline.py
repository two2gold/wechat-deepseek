"""回复策略测试：验证「5 秒内被动回复 / 超时转客服推送」这条核心链路。

这里用桩件替换 DeepSeek 和微信接口，不依赖任何外部网络。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from app.config import settings
from app.memory import RateLimiter, SeenCache, SessionStore
from app.pipeline import ChatPipeline


class StubDeepSeek:
    def __init__(self, delay: float = 0.0, text: str = "这是答案") -> None:
        self.delay = delay
        self.text = text
        self.calls = 0

    def build_messages(self, history, user_text):
        return [{"role": "user", "content": user_text}]

    async def chat(self, messages):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return self.text

    async def aclose(self) -> None:
        pass


class StubWeChatAPI:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, openid: str, content: str) -> None:
        self.sent.append((openid, content))

    async def aclose(self) -> None:
        pass


def build_pipeline(tmp_path, *, mode: str, timeout: float, delay: float, can_push: bool = True):
    store = SessionStore(tmp_path / "pipe.db", history_turns=4, ttl=3600)
    config = replace(settings, reply_mode=mode, passive_timeout=timeout)
    deepseek = StubDeepSeek(delay=delay)
    api = StubWeChatAPI(enabled=can_push)
    pipeline = ChatPipeline(config, store, SeenCache(60), RateLimiter(0, 100), deepseek, api)
    return pipeline, store, deepseek, api


def text_message(openid: str = "oUser1") -> dict[str, str]:
    return {
        "ToUserName": "gh_test",
        "FromUserName": openid,
        "MsgType": "text",
        "Content": "你好",
        "MsgId": "123456789",
    }


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- hybrid
def test_hybrid_fast_answer_is_passive(tmp_path):
    async def scenario():
        pipeline, store, deepseek, api = build_pipeline(
            tmp_path, mode="hybrid", timeout=3.0, delay=0.05
        )
        try:
            reply = await pipeline.handle(text_message())
            await asyncio.sleep(0.05)
            return reply, api.sent, deepseek.calls, store
        finally:
            await pipeline.aclose()

    reply, sent, calls, _ = run(scenario())
    assert reply == "这是答案"
    assert sent == []  # 被动回复成功，不应再推送
    assert calls == 1


def test_hybrid_timeout_replies_ack_and_pushes(tmp_path):
    async def scenario():
        pipeline, store, deepseek, api = build_pipeline(
            tmp_path, mode="hybrid", timeout=0.2, delay=0.6
        )
        try:
            reply = await pipeline.handle(text_message())
            during = list(api.sent)
            await asyncio.sleep(0.8)  # 等后台任务把答案推出去
            return reply, during, api.sent, deepseek.calls, store
        finally:
            await pipeline.aclose()

    reply, during, after, calls, store = run(scenario())
    assert reply == settings.ack_text  # 先给用户一个即时反馈
    assert during == []  # 此刻答案还没生成
    assert after == [("oUser1", "这是答案")]  # 生成后由客服消息补发
    assert calls == 1  # 只调用一次 DeepSeek，没有重复计费
    roles = [item["role"] for item in store.history("oUser1")]
    assert roles == ["user", "assistant"]


def test_hybrid_timeout_without_push_falls_back_to_text(tmp_path):
    async def scenario():
        pipeline, *_ = build_pipeline(
            tmp_path, mode="hybrid", timeout=0.2, delay=0.5, can_push=False
        )
        try:
            return await pipeline.handle(text_message())
        finally:
            await pipeline.aclose()

    assert run(scenario()) == settings.fallback_text


# ---------------------------------------------------------------- 其它模式
def test_passive_mode_never_pushes(tmp_path):
    async def scenario():
        pipeline, _, _, api = build_pipeline(tmp_path, mode="passive", timeout=0.2, delay=0.5)
        try:
            reply = await pipeline.handle(text_message())
            await asyncio.sleep(0.6)
            return reply, api.sent
        finally:
            await pipeline.aclose()

    reply, sent = run(scenario())
    assert reply == settings.fallback_text
    assert sent == []


def test_push_mode_returns_none_then_pushes(tmp_path):
    async def scenario():
        pipeline, _, _, api = build_pipeline(tmp_path, mode="push", timeout=3.0, delay=0.05)
        try:
            reply = await pipeline.handle(text_message())
            await asyncio.sleep(0.2)
            return reply, api.sent
        finally:
            await pipeline.aclose()

    reply, sent = run(scenario())
    assert reply is None  # 服务端返回 "success"，不被动回复
    assert sent == [("oUser1", "这是答案")]


def test_instant_ack_mode(tmp_path):
    async def scenario():
        pipeline, _, _, api = build_pipeline(tmp_path, mode="instant_ack", timeout=3.0, delay=0.05)
        try:
            reply = await pipeline.handle(text_message())
            await asyncio.sleep(0.2)
            return reply, api.sent
        finally:
            await pipeline.aclose()

    reply, sent = run(scenario())
    assert reply == settings.ack_text
    assert sent == [("oUser1", "这是答案")]


# ---------------------------------------------------------------- 去重与限流
def test_duplicate_msg_id_only_calls_deepseek_once(tmp_path):
    async def scenario():
        pipeline, _, deepseek, _ = build_pipeline(tmp_path, mode="hybrid", timeout=3.0, delay=0.0)
        try:
            first = await pipeline.handle(text_message())
            second = await pipeline.handle(text_message())
            return first, second, deepseek.calls
        finally:
            await pipeline.aclose()

    first, second, calls = run(scenario())
    assert first == "这是答案"
    assert second is None  # 微信重试的重复消息被丢弃
    assert calls == 1


def test_rate_limit_replies_busy(tmp_path):
    async def scenario():
        store = SessionStore(tmp_path / "rl.db", history_turns=2, ttl=3600)
        config = replace(settings, reply_mode="hybrid", passive_timeout=3.0)
        pipeline = ChatPipeline(
            config, store, SeenCache(60), RateLimiter(5.0, 100), StubDeepSeek(), StubWeChatAPI()
        )
        try:
            first = await pipeline.handle(text_message())
            msg = text_message()
            msg["MsgId"] = "999"  # 换 MsgId，只触发限流而不是去重
            second = await pipeline.handle(msg)
            return first, second
        finally:
            await pipeline.aclose()

    first, second = run(scenario())
    assert first == "这是答案"
    assert second == settings.busy_text


# ---------------------------------------------------------------- 白名单
def test_whitelist_blocks_unknown_user(tmp_path):
    async def scenario():
        store = SessionStore(tmp_path / "wl.db", history_turns=2, ttl=3600)
        config = replace(
            settings,
            reply_mode="hybrid",
            passive_timeout=3.0,
            allowed_openids=frozenset({"oAllowed"}),
        )
        pipeline = ChatPipeline(
            config, store, SeenCache(60), RateLimiter(0, 100), StubDeepSeek(), StubWeChatAPI()
        )
        try:
            await pipeline.handle(text_message("oAllowed"))
            blocked = await pipeline.handle(text_message("oStranger"))
            # 订阅事件不受白名单影响
            event = {
                "ToUserName": "gh_test",
                "FromUserName": "oStranger",
                "MsgType": "event",
                "Event": "subscribe",
            }
            welcome = await pipeline.handle(event)
            return blocked, welcome, store
        finally:
            await pipeline.aclose()

    blocked, welcome, store = run(scenario())
    assert blocked == settings.not_allowed_text
    assert welcome == settings.welcome_text
    assert store.history("oStranger") == []  # 被拦的消息不写库、不调用 DeepSeek


def test_whitelist_empty_means_everyone_allowed(tmp_path):
    async def scenario():
        pipeline, _, deepseek, _ = build_pipeline(tmp_path, mode="hybrid", timeout=3.0, delay=0.0)
        try:
            reply = await pipeline.handle(text_message("oAnyone"))
            return reply, deepseek.calls
        finally:
            await pipeline.aclose()

    reply, calls = run(scenario())
    assert reply == "这是答案"
    assert calls == 1


# ---------------------------------------------------------------- 事件
@pytest.mark.parametrize(
    "event,expect_welcome",
    [("subscribe", True), ("unsubscribe", False)],
)
def test_events(tmp_path, event, expect_welcome):
    async def scenario():
        pipeline, store, _, _ = build_pipeline(tmp_path, mode="hybrid", timeout=3.0, delay=0.0)
        try:
            msg = {
                "ToUserName": "gh_test",
                "FromUserName": "oEventUser",
                "MsgType": "event",
                "Event": event,
            }
            return await pipeline.handle(msg)
        finally:
            await pipeline.aclose()

    reply = run(scenario())
    if expect_welcome:
        assert reply == settings.welcome_text
    else:
        assert reply is None
