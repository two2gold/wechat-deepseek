"""DeepSeek 客户端（OpenAI 兼容协议）。

接口：POST {base_url}/chat/completions
鉴权：Authorization: Bearer <DEEPSEEK_API_KEY>
模型：deepseek-chat（V3，通用对话）/ deepseek-reasoner（R1，深度推理）
"""

from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}
REASONER_MODELS = {"deepseek-reasoner", "deepseek-r1"}


class DeepSeekError(Exception):
    """调用 DeepSeek 失败。"""

    def __init__(self, message: str, *, retriable: bool = False) -> None:
        super().__init__(message)
        self.retriable = retriable


class DeepSeekClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        *,
        temperature: float = 1.0,
        max_tokens: int = 800,
        timeout: float = 45.0,
        system_prompt: str = "",
        mock: bool = False,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or "https://api.deepseek.com").rstrip("/")
        self.url = f"{self.base_url}/chat/completions"
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.system_prompt = system_prompt
        self.mock = mock
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=8.0, read=timeout, write=15.0, pool=5.0),
            headers=headers,
        )

    @property
    def ready(self) -> bool:
        return self.mock or bool(self.api_key)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def chat(self, messages: list[dict[str, str]]) -> str:
        """传入完整 messages（含 system），返回助手回复文本。"""
        if self.mock:
            last_user = next(
                (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
            )
            await asyncio.sleep(0.05)
            return f"【MOCK 回复】我收到了你的消息：「{last_user}」"

        if not self.api_key:
            raise DeepSeekError("未配置 DEEPSEEK_API_KEY")

        payload: dict = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "max_tokens": self.max_tokens,
        }
        # deepseek-reasoner 不支持采样参数，传了不生效还可能报错
        if self.model not in REASONER_MODELS:
            payload["temperature"] = self.temperature

        last_error = "未知错误"
        for attempt in range(1, 4):
            try:
                resp = await self._client.post(self.url, json=payload)
            except httpx.TimeoutException:
                last_error = "请求 DeepSeek 超时"
                logger.warning("DeepSeek 超时（第 %s 次）", attempt)
            except httpx.HTTPError as exc:
                last_error = f"网络错误：{exc}"
                logger.warning("DeepSeek 网络错误（第 %s 次）：%s", attempt, exc)
            else:
                if resp.status_code == 200:
                    return self._extract(resp.json())
                body = resp.text[:300]
                if resp.status_code in RETRY_STATUS:
                    last_error = f"HTTP {resp.status_code}：{body}"
                    logger.warning("DeepSeek 返回 %s（第 %s 次）", resp.status_code, attempt)
                elif resp.status_code == 401:
                    raise DeepSeekError("DeepSeek 鉴权失败（401）：请检查 DEEPSEEK_API_KEY")
                elif resp.status_code == 402:
                    raise DeepSeekError("DeepSeek 余额不足（402）：请到官网充值")
                else:
                    raise DeepSeekError(f"DeepSeek 返回 HTTP {resp.status_code}：{body}")

            if attempt < 3:
                await asyncio.sleep(0.8 * attempt)

        raise DeepSeekError(last_error, retriable=True)

    @staticmethod
    def _extract(data: dict) -> str:
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise DeepSeekError(f"DeepSeek 响应结构异常：{str(data)[:300]}") from exc
        content = (message.get("content") or "").strip()
        if not content:
            reasoning = (message.get("reasoning_content") or "").strip()
            if reasoning:
                # 只返回了思维链（极少见），退化为截取一段
                content = reasoning[-800:]
        if not content:
            raise DeepSeekError("DeepSeek 返回了空内容")
        return content

    def build_messages(self, history: list[dict[str, str]], user_text: str) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.extend(history)
        messages.append({"role": "user", "content": user_text})
        return messages
