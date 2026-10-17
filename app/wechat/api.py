"""微信开放接口封装：access_token 缓存 + 客服消息推送。

客服消息（custom/send）是突破「5 秒被动回复」限制的关键：
用户发消息后 48 小时内，我们可以主动向该用户推送消息。
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.weixin.qq.com"
TOKEN_REFRESH_MARGIN = 300  # 提前 5 分钟刷新 token


class WeChatAPIError(Exception):
    """调用微信接口失败。"""

    def __init__(self, message: str, *, errcode: int | None = None) -> None:
        super().__init__(message)
        self.errcode = errcode


class WeChatAPI:
    """access_token 由微信侧统一颁发，必须缓存复用，否则会被限流。"""

    def __init__(self, appid: str, appsecret: str, timeout: float = 15.0) -> None:
        self._appid = (appid or "").strip()
        self._appsecret = (appsecret or "").strip()
        self._client = httpx.AsyncClient(base_url=API_BASE, timeout=timeout)
        self._token: str = ""
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self._appid and self._appsecret)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ----------------------------------------------------------- token
    async def access_token(self, *, force: bool = False) -> str:
        if not self.enabled:
            raise WeChatAPIError("未配置 WECHAT_APPID / WECHAT_APPSECRET，无法调用微信接口")
        async with self._lock:
            now = time.time()
            if not force and self._token and now < self._expires_at:
                return self._token
            try:
                resp = await self._client.get(
                    "/cgi-bin/token",
                    params={
                        "grant_type": "client_credential",
                        "appid": self._appid,
                        "secret": self._appsecret,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPError as exc:
                raise WeChatAPIError(f"获取 access_token 网络失败：{exc}") from exc

            if "access_token" not in data:
                raise WeChatAPIError(
                    f"获取 access_token 失败：{data}", errcode=data.get("errcode")
                )
            self._token = data["access_token"]
            self._expires_at = now + int(data.get("expires_in", 7200)) - TOKEN_REFRESH_MARGIN
            logger.info("access_token 已刷新，%s 秒后过期", int(self._expires_at - now))
            return self._token

    # ------------------------------------------------------- 客服消息
    async def send_text(self, openid: str, content: str) -> None:
        """给用户推送一条客服文本消息。"""
        await self._send(
            {
                "touser": openid,
                "msgtype": "text",
                "text": {"content": content},
            }
        )

    async def _send(self, payload: dict) -> None:
        for attempt in (1, 2):
            token = await self.access_token()
            try:
                resp = await self._client.post(
                    "/cgi-bin/message/custom/send",
                    params={"access_token": token},
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPError as exc:
                raise WeChatAPIError(f"客服消息网络失败：{exc}") from exc

            errcode = data.get("errcode", 0)
            if errcode == 0:
                return
            # 40001/42001 = token 失效，强刷一次再试
            if errcode in (40001, 42001) and attempt == 1:
                logger.warning("access_token 失效（errcode=%s），强制刷新后重试", errcode)
                await self.access_token(force=True)
                continue
            raise WeChatAPIError(f"客服消息发送失败：{data}", errcode=errcode)
        raise WeChatAPIError("客服消息发送失败：token 重试后仍未成功")
