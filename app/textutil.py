"""文本处理小工具：按字节截断、去 Markdown、日志脱敏。"""

from __future__ import annotations

import re

# 代码块 / 行内代码：先摘出来保护，避免内部内容被当成 Markdown 处理
_FENCE_RE = re.compile(r"```[^\n`]*\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
# 粗体：**x**
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
# 斜体：必须左右都不是单词字符，否则 read_csv / __init__ 这类标识符会被吃掉
_ITALIC_STAR_RE = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_ITALIC_UNDER_RE = re.compile(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])")
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}(?=[ \t])[ \t]*", re.MULTILINE)
_QUOTE_RE = re.compile(r"^[ \t]{0,3}>[ \t]?", re.MULTILINE)
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((?:[^)\n]+)\)")
_BLANK_RE = re.compile(r"\n{3,}")


def strip_markdown(text: str) -> str:
    """把常见 Markdown 语法降级成微信里看着舒服的纯文本。

    要点：代码块与行内代码先"摘出来"占位，最后原样放回，
    这样 ``pd.read_csv`` / ``__init__`` / ``snake_case`` 不会被斜体规则误伤。
    """
    stash: list[str] = []

    def _stash(match: re.Match) -> str:
        stash.append(match.group(1))
        return f"\x00{len(stash) - 1}\x00"

    out = _FENCE_RE.sub(_stash, text)
    out = _INLINE_CODE_RE.sub(_stash, out)
    out = _BOLD_RE.sub(lambda m: m.group(1), out)
    out = _ITALIC_STAR_RE.sub(lambda m: m.group(1), out)
    out = _ITALIC_UNDER_RE.sub(lambda m: m.group(1), out)
    out = _HEADING_RE.sub("", out)
    out = _QUOTE_RE.sub("", out)
    out = _LINK_RE.sub(r"\1", out)
    out = _BLANK_RE.sub("\n\n", out)

    for index, code in enumerate(stash):
        out = out.replace(f"\x00{index}\x00", code)
    return out.strip()


def truncate_bytes(text: str, limit: int, suffix: str = "……（内容过长已截断）") -> str:
    """按 UTF-8 字节数截断，且不会把一个汉字劈成两半。

    微信被动回复与客服消息的文本上限都是 2048 字节，所以按字节算才安全。
    """
    if limit <= 0:
        return ""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    suffix_bytes = suffix.encode("utf-8")
    budget = max(0, limit - len(suffix_bytes))
    cut = raw[:budget]
    # 回退到合法的 UTF-8 边界
    while cut:
        try:
            head = cut.decode("utf-8")
            break
        except UnicodeDecodeError:
            cut = cut[:-1]
    else:
        head = ""
    return head + suffix


def split_by_bytes(text: str, limit: int) -> list[str]:
    """把长文本切成若干块，每块不超过 limit 字节，优先在换行/句号处断开。"""
    if limit <= 0 or len(text.encode("utf-8")) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    rest = text
    while rest and len(rest.encode("utf-8")) > limit:
        raw = rest.encode("utf-8")[:limit]
        while raw:
            try:
                head = raw.decode("utf-8")
                break
            except UnicodeDecodeError:
                raw = raw[:-1]
        else:
            head = ""
        if not head:  # 理论上不会发生，保底避免死循环
            break
        break_at = max(head.rfind("\n"), head.rfind("。"), head.rfind("！"), head.rfind("？"))
        if break_at < limit // 3:  # 断点太靠前就不切了，直接硬切
            break_at = len(head) - 1
        chunks.append(head[: break_at + 1].strip())
        rest = rest[break_at + 1 :].lstrip()
    if rest:
        chunks.append(rest)
    return [c for c in chunks if c]


def mask_id(value: str, keep: int = 6, tail: int = 3) -> str:
    """日志里脱敏 openid / MsgId，避免把用户标识落到日志文件。

    要保留尾部特征：微信同一公众号下不同用户的 openid 可能共享相同前缀
    （实测有 on6wW3... 开头的多个用户），只留前缀会让不同人看起来一模一样。
    """
    if not value:
        return ""
    if len(value) <= keep + tail:
        return value[0] + "***"
    return f"{value[:keep]}***{value[-tail:]}({len(value)})"


def normalize_input(text: str, max_chars: int) -> str:
    """清理用户输入：去掉零宽字符、压缩空白、限制长度。"""
    cleaned = (
        (text or "")
        .replace("\u200b", "")
        .replace("\ufeff", "")
        .replace("\r\n", "\n")
        .strip()
    )
    if max_chars > 0 and len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars]
    return cleaned
