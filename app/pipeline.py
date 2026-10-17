"""消息处理主流程：把微信消息接到 DeepSeek，并把答案送回用户。

核心难点是微信的 5 秒硬限制（超时会重试 3 次，用户侧显示"该公众号暂时无法提供服务"）：
DeepSeek 生成通常要 3~10 秒，所以这里提供四种策略（REPLY_MODE）：

- hybrid（默认）：先等 PASSIVE_TIMEOUT 秒，答完就正常被动回复；
  没答完就先回一句 ACK，然后通过客服消息把正式答案推过去。
- instant_ack：立刻回 ACK，答案全部走客服消息推送。
- push：不被动回复，直接异步推送答案（用户只会看到一条消息）。
- passive：只走被动回复，超时就回固定兜底文案（不调用客服接口，适合未认证/测试号兜底）。
"""

from __future__ import annotations

import asyncio
import logging
import time

from .config import Settings
from .deepseek import DeepSeekClient, DeepSeekError
from .memory import RateLimiter, SeenCache, SessionStore
from .textutil import mask_id, normalize_input, split_by_bytes, strip_markdown, truncate_bytes
from .wechat.api import WeChatAPI, WeChatAPIError

logger = logging.getLogger(__name__)

ACK_ONLY_TEXT = "🤔 正在思考中，请稍等……"
ERROR_TEXT = "抱歉，我这边调用 AI 出了点问题，请稍后再试一次～"


class ChatPipeline:
    def __init__(
        self,
        settings: Settings,
        store: SessionStore,
        seen: SeenCache,
        limiter: RateLimiter,
        deepseek: DeepSeekClient,
        wechat_api: WeChatAPI,
    ) -> None:
        self.settings = settings
        self.store = store
        self.seen = seen
        self.limiter = limiter
        self.deepseek = deepseek
        self.wechat_api = wechat_api
        self._sem = asyncio.Semaphore(max(1, settings.max_concurrency))
        self._background: set[asyncio.Task] = set()

    # ------------------------------------------------------------ 生命周期
    async def aclose(self) -> None:
        for task in list(self._background):
            task.cancel()
        self._background.clear()
        await self.deepseek.aclose()
        await self.wechat_api.aclose()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    # ---------------------------------------------------------------- 入口
    async def handle(self, msg: dict[str, str]) -> str | None:
        """返回要被动回复的文本；返回 None 表示回 "success"（不被动回复）。"""
        msg_type = msg.get("MsgType", "")
        openid = msg.get("FromUserName", "")

        if msg_type == "event":
            return self._handle_event(msg)

        # 白名单：ALLOWED_OPENIDS 留空表示不限制；只拦消息，不拦关注/取关事件
        if self.settings.allowed_openids and openid not in self.settings.allowed_openids:
            logger.info("非白名单用户已被拒绝 openid=%s", mask_id(openid))
            return self.settings.not_allowed_text

        if msg_type != "text":
            logger.info("收到非文本消息 type=%s", msg_type)
            return self.settings.unsupported_text

        msg_id = msg.get("MsgId", "")
        if msg_id and not self.seen.mark(f"msg:{msg_id}"):
            logger.info("重复推送已忽略 MsgId=%s", mask_id(msg_id))
            return None

        user_text = normalize_input(msg.get("Content", ""), self.settings.max_input_chars)
        if not user_text:
            return "你好像什么也没说哦，直接输入问题就行～"

        allowed, reason = self.limiter.allow(openid)
        if not allowed:
            logger.info("触发限流 openid=%s reason=%s", mask_id(openid), reason)
            return self.settings.busy_text if reason == "too_fast" else "今天聊得有点多啦，休息一会儿再来找我吧～"

        return await self._dispatch(openid, user_text)

    # ------------------------------------------------------------ 事件处理
    def _handle_event(self, msg: dict[str, str]) -> str | None:
        event = (msg.get("Event") or "").lower()
        openid = msg.get("FromUserName", "")
        if event == "subscribe":
            self.store.clear(openid)
            logger.info("新关注用户 openid=%s", mask_id(openid))
            return self.settings.welcome_text
        if event == "unsubscribe":
            self.store.clear(openid)
            logger.info("用户取关 openid=%s", mask_id(openid))
            return None
        if event == "click":
            return "收到你的点击～直接把问题发给我就行。"
        logger.info("忽略事件 Event=%s", event)
        return None

    # ------------------------------------------------------------ 策略分发
    async def _dispatch(self, openid: str, user_text: str) -> str | None:
        mode = self.settings.reply_mode
        if mode not in {"hybrid", "instant_ack", "push", "passive"}:
            mode = "hybrid"
        can_push = self.wechat_api.enabled

        task = self._spawn(self._answer(openid, user_text))

        if mode == "passive":
            return await self._wait_or_default(task)

        if mode == "push":
            if can_push:
                self._spawn(self._push_answer(task, openid))
                return None
            logger.warning("REPLY_MODE=push 但未配置 AppID/AppSecret，退化为被动回复等待")
            return await self._wait_or_default(task)

        if mode == "instant_ack":
            if can_push:
                self._spawn(self._push_answer(task, openid))
                return self.settings.ack_text
            return await self._wait_or_default(task)

        # hybrid：先抢被动回复，抢不到再推送
        try:
            return await asyncio.wait_for(
                asyncio.shield(task), timeout=self.settings.passive_timeout
            )
        except asyncio.TimeoutError:
            if can_push:
                logger.info(
                    "被动回复 %.1fs 内未完成，转客服消息推送 openid=%s",
                    self.settings.passive_timeout,
                    mask_id(openid),
                )
                self._spawn(self._push_answer(task, openid))
                return self.settings.ack_text
            logger.warning("被动回复超时且无法推送，返回兜底文案")
            return self.settings.fallback_text

    async def _wait_or_default(self, task: asyncio.Task) -> str:
        try:
            return await asyncio.wait_for(
                asyncio.shield(task), timeout=self.settings.passive_timeout
            )
        except asyncio.TimeoutError:
            return self.settings.fallback_text

    # ------------------------------------------------------------ 生成答案
    async def _answer(self, openid: str, user_text: str) -> str:
        started = time.time()
        history = self.store.history(openid)
        messages = self.deepseek.build_messages(history, user_text)
        self.store.append(openid, "user", user_text)

        try:
            async with self._sem:
                raw = await self.deepseek.chat(messages)
        except DeepSeekError as exc:
            logger.error("DeepSeek 调用失败：%s", exc)
            return ERROR_TEXT
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 兜底，绝不让异常冒到微信侧
            logger.exception("DeepSeek 调用异常：%s", exc)
            return ERROR_TEXT

        reply = self._format_reply(raw)
        self.store.append(openid, "assistant", reply)
        logger.info(
            "生成完成 openid=%s 耗时=%.2fs 长度=%s",
            mask_id(openid),
            time.time() - started,
            len(reply),
        )
        return reply

    def _format_reply(self, raw: str) -> str:
        text = (raw or "").strip()
        if self.settings.strip_markdown:
            text = strip_markdown(text)
        text = truncate_bytes(text, self.settings.max_reply_bytes)
        return text or ERROR_TEXT

    # ------------------------------------------------------- 客服消息推送
    async def _push_answer(self, task: asyncio.Task, openid: str) -> None:
        try:
            answer = await task
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("后台生成失败：%s", exc)
            answer = ERROR_TEXT

        # 客服消息文本上限同样是 2048 字节，超长按块发送
        chunks = split_by_bytes(answer, self.settings.max_reply_bytes)
        if not chunks:
            return
        if len(chunks) > self.settings.push_max_chunks:
            chunks = chunks[: self.settings.push_max_chunks]
        for index, chunk in enumerate(chunks, start=1):
            try:
                await self.wechat_api.send_text(openid, chunk)
                logger.info("客服消息已推送 openid=%s 第 %s/%s 块", mask_id(openid), index, len(chunks))
            except WeChatAPIError as exc:
                logger.error("客服消息推送失败 openid=%s：%s", mask_id(openid), exc)
                return
            await asyncio.sleep(0.3)  # 轻微间隔，降低触发频控的概率
