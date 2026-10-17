"""微信消息 XML 的解析与构造。

微信的 XML 结构简单且固定，用标准库 ElementTree 即可；
ElementTree 默认不解析外部实体，不存在 XXE 风险。
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET


class WeChatXMLError(Exception):
    """XML 解析失败。"""


def parse_message(xml_data: str | bytes) -> dict[str, str]:
    """把微信推送的 XML 拍平成 {标签: 文本} 字典。"""
    if isinstance(xml_data, str):
        xml_data = xml_data.encode("utf-8")
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as exc:
        raise WeChatXMLError(f"XML 解析失败：{exc}") from exc
    result: dict[str, str] = {}
    for child in root:
        result[child.tag] = child.text or ""
    return result


def _cdata(text: str) -> str:
    """CDATA 包裹，并转义内部可能出现的 ]]> 结束符。"""
    safe = str(text).replace("]]>", "]]]]><![CDATA[>")
    return f"<![CDATA[{safe}]]>"


def text_reply_xml(to_user: str, from_user: str, content: str, create_time: int | None = None) -> str:
    """构造被动回复的文本消息 XML。"""
    ts = int(create_time if create_time is not None else time.time())
    return (
        "<xml>"
        f"<ToUserName>{_cdata(to_user)}</ToUserName>"
        f"<FromUserName>{_cdata(from_user)}</FromUserName>"
        f"<CreateTime>{ts}</CreateTime>"
        f"<MsgType>{_cdata('text')}</MsgType>"
        f"<Content>{_cdata(content)}</Content>"
        "</xml>"
    )


def encrypted_reply_xml(encrypt: str, signature: str, timestamp: str, nonce: str) -> str:
    """安全模式下，被动回复要把密文再包一层。"""
    return (
        "<xml>"
        f"<Encrypt>{_cdata(encrypt)}</Encrypt>"
        f"<MsgSignature>{_cdata(signature)}</MsgSignature>"
        f"<TimeStamp>{int(timestamp)}</TimeStamp>"
        f"<Nonce>{_cdata(nonce)}</Nonce>"
        "</xml>"
    )
