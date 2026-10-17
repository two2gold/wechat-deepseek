"""FastAPI 入口：接收微信服务器推送，返回被动回复。

路由：
- GET  /wechat   微信后台"服务器配置"的 URL 校验
- POST /wechat   消息与事件推送
- GET  /healthz  健康检查（内网穿透/负载均衡探活）
- GET  /         给人看的说明页
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from .config import settings
from .deepseek import DeepSeekClient
from .memory import RateLimiter, SeenCache, SessionStore
from .pipeline import ChatPipeline
from .wechat.api import WeChatAPI
from .wechat.crypto import WeChatCrypto, WeChatCryptoError
from .wechat.xml import (
    WeChatXMLError,
    encrypted_reply_xml,
    parse_message,
    text_reply_xml,
)

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 512 * 1024  # 微信推送的消息很小，超过一定拒绝


def configure_logging() -> None:
    # Windows 中文控制台默认 GBK，日志里出现 emoji 时会让 logging 报编码错误
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def _purge_loop(store: SessionStore) -> None:
    while True:
        await asyncio.sleep(3600)
        try:
            removed = await asyncio.to_thread(store.purge_expired)
            if removed:
                logger.info("清理过期会话记录 %s 条", removed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("清理会话失败：%s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    for warning in settings.validate():
        logger.warning("配置提醒：%s", warning)

    try:
        crypto = WeChatCrypto(settings.wechat_token, settings.wechat_aes_key, settings.wechat_appid)
    except WeChatCryptoError as exc:
        logger.error("加解密初始化失败，降级为仅明文模式：%s", exc)
        crypto = WeChatCrypto(settings.wechat_token)

    store = SessionStore(settings.db_path, settings.history_turns, settings.session_ttl)
    deepseek = DeepSeekClient(
        settings.deepseek_api_key,
        settings.deepseek_base_url,
        settings.deepseek_model,
        temperature=settings.deepseek_temperature,
        max_tokens=settings.deepseek_max_tokens,
        timeout=settings.deepseek_timeout,
        system_prompt=settings.deepseek_system_prompt,
        mock=settings.deepseek_mock,
    )
    wechat_api = WeChatAPI(settings.wechat_appid, settings.wechat_appsecret)
    pipeline = ChatPipeline(
        settings,
        store,
        SeenCache(settings.dedupe_ttl),
        RateLimiter(settings.rate_limit_seconds, settings.rate_limit_per_hour),
        deepseek,
        wechat_api,
    )

    app.state.crypto = crypto
    app.state.store = store
    app.state.pipeline = pipeline
    app.state.started_at = time.time()
    app.state.purge_task = asyncio.create_task(_purge_loop(store))

    logger.info(
        "服务已启动 | 模型=%s | 回复策略=%s | 安全模式=%s | 客服推送=%s | 模拟=%s",
        settings.deepseek_model,
        settings.reply_mode,
        "开启" if crypto.safe_mode_enabled else "关闭",
        "可用" if wechat_api.enabled else "不可用",
        "是" if settings.deepseek_mock else "否",
    )
    try:
        yield
    finally:
        app.state.purge_task.cancel()
        await pipeline.aclose()
        store.close()
        logger.info("服务已停止")


app = FastAPI(title="WeChat Official Account × DeepSeek", version="1.0.0", lifespan=lifespan)


@app.get("/", include_in_schema=False)
async def index() -> JSONResponse:
    return JSONResponse(
        {
            "service": "wechat-deepseek",
            "status": "ok",
            "verify_url": "/wechat",
            "model": settings.deepseek_model,
            "reply_mode": settings.reply_mode,
            "mock": settings.deepseek_mock,
        }
    )


@app.get("/healthz", include_in_schema=False)
async def healthz(request: Request) -> JSONResponse:
    state = request.app.state
    started = getattr(state, "started_at", time.time())
    return JSONResponse(
        {
            "status": "ok",
            "uptime_seconds": round(time.time() - started, 1),
            "users": state.store.count_users(),
            "active_tasks": len(state.pipeline._background),  # noqa: SLF001 - 仅用于观测
            "safe_mode": state.crypto.safe_mode_enabled,
            "push_enabled": state.pipeline.wechat_api.enabled,
            "deepseek_mock": settings.deepseek_mock,
        }
    )


@app.get("/wechat", include_in_schema=False)
async def wechat_verify(
    request: Request,
    signature: str = "",
    timestamp: str = "",
    nonce: str = "",
    echostr: str = "",
) -> Response:
    """微信后台点击"提交"时，会带上 signature/timestamp/nonce/echostr 来校验。"""
    crypto: WeChatCrypto = request.app.state.crypto
    if not settings.wechat_token:
        logger.error("WECHAT_TOKEN 未配置，无法完成 URL 校验")
        return PlainTextResponse("WECHAT_TOKEN not configured", status_code=403)
    if crypto.check_signature(signature, timestamp, nonce):
        logger.info("URL 校验通过（signature=%s...）", signature[:8])
        return PlainTextResponse(echostr)
    logger.warning(
        "URL 校验失败：signature=%s timestamp=%s nonce=%s（通常填写的 Token 与 .env 不一致）",
        signature,
        timestamp,
        nonce,
    )
    return PlainTextResponse("invalid signature", status_code=403)


@app.post("/wechat", include_in_schema=False)
async def wechat_message(request: Request) -> Response:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        logger.warning("请求体过大：%s 字节，已忽略", len(raw))
        return PlainTextResponse("success")

    state = request.app.state
    crypto: WeChatCrypto = state.crypto
    params = request.query_params
    encrypt_type = params.get("encrypt_type", "")
    timestamp = params.get("timestamp", "")
    nonce = params.get("nonce", "")

    # ---------- 1. 校验 + 解密 ----------
    try:
        if encrypt_type == "aes":
            if not crypto.safe_mode_enabled:
                logger.error("收到安全模式消息，但 WECHAT_AES_KEY 未配置")
                return PlainTextResponse("invalid", status_code=403)
            outer = parse_message(raw)
            encrypt = outer.get("Encrypt", "")
            if not crypto.check_msg_signature(
                params.get("msg_signature", ""), timestamp, nonce, encrypt
            ):
                logger.warning("安全模式 msg_signature 校验失败")
                return PlainTextResponse("invalid signature", status_code=403)
            xml_text = crypto.decrypt(encrypt)
        else:
            if not crypto.check_signature(params.get("signature", ""), timestamp, nonce):
                logger.warning("明文模式 signature 校验失败")
                return PlainTextResponse("invalid signature", status_code=403)
            xml_text = raw.decode("utf-8", errors="replace")
        msg = parse_message(xml_text)
    except (WeChatXMLError, WeChatCryptoError) as exc:
        logger.error("消息解析失败：%s", exc)
        return PlainTextResponse("success")
    except Exception as exc:  # noqa: BLE001
        logger.exception("处理请求时发生未知错误：%s", exc)
        return PlainTextResponse("success")

    msg_type = msg.get("MsgType", "")
    logger.info("收到消息 type=%s event=%s", msg_type, msg.get("Event", ""))

    # ---------- 2. 交给业务处理，任何异常都回 success，避免微信报错刷屏 ----------
    try:
        reply = await state.pipeline.handle(msg)
    except Exception as exc:  # noqa: BLE001
        logger.exception("业务处理失败：%s", exc)
        return PlainTextResponse("success")

    if not reply:
        return PlainTextResponse("success")

    # ---------- 3. 组装被动回复 ----------
    body = text_reply_xml(msg.get("FromUserName", ""), msg.get("ToUserName", ""), reply)
    if encrypt_type == "aes":
        try:
            encrypted = crypto.encrypt(body)
            reply_ts = str(int(time.time()))
            reply_nonce = nonce or str(int(time.time() * 1000))
            body = encrypted_reply_xml(
                encrypted, crypto.msg_signature(reply_ts, reply_nonce, encrypted), reply_ts, reply_nonce
            )
        except WeChatCryptoError as exc:
            logger.error("加密回复失败：%s", exc)
            return PlainTextResponse("success")
    return Response(content=body, media_type="application/xml")
