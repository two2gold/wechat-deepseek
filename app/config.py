"""集中配置：全部通过环境变量或项目根目录的 .env 注入。

设计原则：
1. 不引入 pydantic-settings / python-dotenv 等额外依赖，自带极简 .env 解析；
2. 所有配置项都有安全默认值，只有 WECHAT_TOKEN 和 DEEPSEEK_API_KEY 属于必填；
3. 导入本模块即完成 .env 加载，并暴露单例 ``settings``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_SYSTEM_PROMPT = (
    "你是一个通过微信公众号为用户解答问题的 AI 助手。"
    "请用简洁、友好、口语化的中文回答，尽量控制在 300 字以内。"
    "不要输出 Markdown 语法（不要用 **、#、表格、代码块围栏），"
    "需要分点时用「1. 2. 3.」或「- 」开头并换行。"
    "如果问题涉及你不确定的信息，请明确说明不确定，不要编造。"
)


def load_dotenv(path: Path | None = None) -> None:
    """极简 .env 加载器；已存在的真实环境变量优先，不会被覆盖。"""
    env_path = path or PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_str(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env_str(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name, "")
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on"}


def _env_openid_set(name: str) -> frozenset[str]:
    """逗号/分号/空格分隔的 openid 白名单；留空表示不限制（所有人都能用）。"""
    raw = _env_str(name, "")
    if not raw:
        return frozenset()
    parts = raw.replace(";", ",").replace(" ", ",").split(",")
    return frozenset(part.strip() for part in parts if part.strip())


@dataclass(frozen=True)
class Settings:
    # ---------- 服务本身 ----------
    host: str
    port: int
    log_level: str

    # ---------- 微信公众号 ----------
    wechat_token: str
    wechat_appid: str
    wechat_appsecret: str
    wechat_aes_key: str

    # ---------- DeepSeek ----------
    deepseek_api_key: str
    deepseek_base_url: str
    deepseek_model: str
    deepseek_temperature: float
    deepseek_max_tokens: int
    deepseek_timeout: float
    deepseek_system_prompt: str
    deepseek_mock: bool

    # ---------- 回复策略 ----------
    reply_mode: str
    passive_timeout: float
    ack_text: str
    fallback_text: str
    busy_text: str
    welcome_text: str
    unsupported_text: str
    max_reply_bytes: int
    max_input_chars: int
    strip_markdown: bool
    push_max_chunks: int

    # ---------- 会话记忆 ----------
    history_turns: int
    session_ttl: int
    db_path: Path

    # ---------- 风控 ----------
    rate_limit_seconds: float
    rate_limit_per_hour: int
    dedupe_ttl: int
    max_concurrency: int
    allowed_openids: frozenset[str]
    not_allowed_text: str
    notify_openids: frozenset[str]

    @classmethod
    def from_env(cls) -> "Settings":
        db_path = Path(_env_str("DB_PATH", "data/sessions.db"))
        if not db_path.is_absolute():
            db_path = PROJECT_ROOT / db_path
        return cls(
            host=_env_str("HOST", "0.0.0.0"),
            port=_env_int("PORT", 8000),
            log_level=_env_str("LOG_LEVEL", "info").lower(),
            wechat_token=_env_str("WECHAT_TOKEN"),
            wechat_appid=_env_str("WECHAT_APPID"),
            wechat_appsecret=_env_str("WECHAT_APPSECRET"),
            wechat_aes_key=_env_str("WECHAT_AES_KEY"),
            deepseek_api_key=_env_str("DEEPSEEK_API_KEY"),
            deepseek_base_url=_env_str("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            deepseek_model=_env_str("DEEPSEEK_MODEL", "deepseek-chat"),
            deepseek_temperature=_env_float("DEEPSEEK_TEMPERATURE", 1.0),
            deepseek_max_tokens=_env_int("DEEPSEEK_MAX_TOKENS", 800),
            deepseek_timeout=_env_float("DEEPSEEK_TIMEOUT", 45.0),
            deepseek_system_prompt=_env_str("DEEPSEEK_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
            deepseek_mock=_env_bool("DEEPSEEK_MOCK", False),
            reply_mode=_env_str("REPLY_MODE", "hybrid").lower(),
            passive_timeout=_env_float("PASSIVE_TIMEOUT", 3.5),
            ack_text=_env_str("ACK_TEXT", "收到啦，正在思考中……请稍等几秒 🧠"),
            fallback_text=_env_str(
                "FALLBACK_TEXT", "这个问题有点费脑，我再想想，稍后把答案发给你～"
            ),
            busy_text=_env_str("BUSY_TEXT", "消息有点密集，休息 1 秒再发一次吧～"),
            welcome_text=_env_str(
                "WELCOME_TEXT", "你好呀！我是接入 DeepSeek 的 AI 助手，直接把问题发给我就行～"
            ),
            unsupported_text=_env_str(
                "UNSUPPORTED_TEXT", "我目前只能读懂文字消息哦，发一段文字试试～"
            ),
            max_reply_bytes=_env_int("MAX_REPLY_BYTES", 1800),
            max_input_chars=_env_int("MAX_INPUT_CHARS", 800),
            strip_markdown=_env_bool("STRIP_MARKDOWN", True),
            push_max_chunks=_env_int("PUSH_MAX_CHUNKS", 3),
            history_turns=_env_int("HISTORY_TURNS", 6),
            session_ttl=_env_int("SESSION_TTL", 3600),
            db_path=db_path,
            rate_limit_seconds=_env_float("RATE_LIMIT_SECONDS", 1.0),
            rate_limit_per_hour=_env_int("RATE_LIMIT_PER_HOUR", 60),
            dedupe_ttl=_env_int("DEDUPE_TTL", 300),
            max_concurrency=_env_int("MAX_CONCURRENCY", 4),
            allowed_openids=_env_openid_set("ALLOWED_OPENIDS"),
            not_allowed_text=_env_str(
                "NOT_ALLOWED_TEXT", "抱歉，这个公众号目前只对指定用户开放～"
            ),
            notify_openids=_env_openid_set("NOTIFY_OPENIDS"),
        )

    # ---------- 派生属性 ----------
    @property
    def has_wechat_credentials(self) -> bool:
        """是否能调用客服消息接口（需要 AppID + AppSecret）。"""
        return bool(self.wechat_appid and self.wechat_appsecret)

    @property
    def has_deepseek_credentials(self) -> bool:
        return bool(self.deepseek_api_key)

    def validate(self) -> list[str]:
        """返回配置告警列表（不阻断启动，方便先本地跑通）。"""
        warnings: list[str] = []
        if not self.wechat_token:
            warnings.append("WECHAT_TOKEN 未配置：微信服务器校验会全部失败。")
        if not self.has_wechat_credentials:
            warnings.append(
                "WECHAT_APPID / WECHAT_APPSECRET 未配置：无法使用客服消息异步推送，"
                "只能靠被动回复（超过 5 秒就会失败）。"
            )
        if not self.has_deepseek_credentials and not self.deepseek_mock:
            warnings.append("DEEPSEEK_API_KEY 未配置：请设置，或打开 DEEPSEEK_MOCK=1 做本地联调。")
        if self.wechat_aes_key and len(self.wechat_aes_key) != 43:
            warnings.append("WECHAT_AES_KEY 长度不是 43：安全模式会解密失败，请核对后台配置。")
        if self.reply_mode not in {"hybrid", "instant_ack", "push", "passive"}:
            warnings.append(f"REPLY_MODE={self.reply_mode} 不是合法值，将按 hybrid 处理。")
        return warnings


load_dotenv()
settings = Settings.from_env()
