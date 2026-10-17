"""微信消息加解密（对应官方 WXBizMsgCrypt）。

支持的两种模式：
- 明文模式 / 兼容模式：只需 SHA1 签名校验；
- 安全模式：额外做 AES-256-CBC 加解密，EncodingAESKey 为 43 位字符串。

AES 依赖 pycryptodome；只有真正用到安全模式时才 import，明文模式零负担。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct

BLOCK_SIZE = 32  # 微信官方示例使用 32 字节分块做 PKCS7 填充


class WeChatCryptoError(Exception):
    """加解密或签名相关的错误。"""


def _sha1_signature(*parts: str) -> str:
    """把若干字符串字典序排序后拼接做 SHA1，微信所有签名都走这个规则。"""
    joined = "".join(sorted(p or "" for p in parts))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


def _load_aes():
    try:
        from Crypto.Cipher import AES  # type: ignore

        return AES
    except ImportError as exc:  # pragma: no cover - 只在缺依赖时触发
        raise WeChatCryptoError(
            "缺少 pycryptodome 依赖，无法使用安全模式。请执行：pip install pycryptodome"
        ) from exc


class WeChatCrypto:
    """封装签名校验与消息加解密。"""

    def __init__(self, token: str, encoding_aes_key: str = "", appid: str = "") -> None:
        self.token = (token or "").strip()
        self.appid = (appid or "").strip()
        self.encoding_aes_key = (encoding_aes_key or "").strip()
        self._key: bytes | None = None
        if self.encoding_aes_key:
            if len(self.encoding_aes_key) != 43:
                raise WeChatCryptoError(
                    "EncodingAESKey 必须是 43 位字符串（公众平台后台生成，勿手改）"
                )
            try:
                self._key = base64.b64decode(self.encoding_aes_key + "=")
            except Exception as exc:  # noqa: BLE001
                raise WeChatCryptoError(f"EncodingAESKey 解析失败：{exc}") from exc
            if len(self._key) != 32:
                raise WeChatCryptoError("EncodingAESKey 解码后不是 32 字节，请核对后台配置")

    # ------------------------------------------------------------------ 签名
    @property
    def safe_mode_enabled(self) -> bool:
        return self._key is not None

    def sign(self, timestamp: str, nonce: str) -> str:
        """URL 校验 / 明文消息的签名（与 check_signature 对应），测试工具会用到。"""
        return _sha1_signature(self.token, timestamp, nonce)

    def check_signature(self, signature: str, timestamp: str, nonce: str) -> bool:
        """校验 URL 接入验证 / 明文消息的 signature。"""
        if not self.token:
            return False
        expected = self.sign(timestamp, nonce)
        return hmac.compare_digest(expected.encode(), (signature or "").encode())

    def msg_signature(self, timestamp: str, nonce: str, encrypt: str) -> str:
        """安全模式下对密文的签名。"""
        return _sha1_signature(self.token, timestamp, nonce, encrypt)

    def check_msg_signature(
        self, msg_signature: str, timestamp: str, nonce: str, encrypt: str
    ) -> bool:
        if not self.token:
            return False
        expected = self.msg_signature(timestamp, nonce, encrypt)
        return hmac.compare_digest(expected.encode(), (msg_signature or "").encode())

    # ------------------------------------------------------------- 加解密
    def encrypt(self, plaintext: str) -> str:
        """明文 XML -> base64 密文。

        结构：random(16) + msg_len(4, 大端) + msg + appid，再做 PKCS7 填充与 AES-256-CBC。
        """
        if self._key is None:
            raise WeChatCryptoError("未配置 WECHAT_AES_KEY，不能加密（安全模式未开启）")
        AES = _load_aes()
        msg = plaintext.encode("utf-8")
        payload = (
            os.urandom(16)
            + struct.pack(">I", len(msg))
            + msg
            + self.appid.encode("utf-8")
        )
        pad_len = BLOCK_SIZE - (len(payload) % BLOCK_SIZE)
        if pad_len == 0:
            pad_len = BLOCK_SIZE
        payload += bytes([pad_len]) * pad_len
        cipher = AES.new(self._key, AES.MODE_CBC, self._key[:16])
        return base64.b64encode(cipher.encrypt(payload)).decode("ascii")

    def decrypt(self, encrypt_b64: str) -> str:
        """base64 密文 -> 明文 XML。"""
        if self._key is None:
            raise WeChatCryptoError("未配置 WECHAT_AES_KEY，不能解密（安全模式未开启）")
        AES = _load_aes()
        try:
            data = base64.b64decode(encrypt_b64)
        except Exception as exc:  # noqa: BLE001
            raise WeChatCryptoError(f"密文不是合法 base64：{exc}") from exc
        if not data or len(data) % 16 != 0:
            raise WeChatCryptoError("密文长度非法，可能 EncodingAESKey 配错")
        cipher = AES.new(self._key, AES.MODE_CBC, self._key[:16])
        plain = cipher.decrypt(data)

        pad_len = plain[-1]
        if pad_len < 1 or pad_len > BLOCK_SIZE:
            raise WeChatCryptoError("PKCS7 填充非法，通常是 EncodingAESKey 不匹配")
        plain = plain[:-pad_len]
        if len(plain) < 20:
            raise WeChatCryptoError("解密结果过短，数据可能已损坏")

        content = plain[16:]
        msg_len = struct.unpack(">I", content[:4])[0]
        if msg_len < 0 or msg_len > len(content) - 4:
            raise WeChatCryptoError("消息长度字段非法，通常是 EncodingAESKey 不匹配")
        try:
            msg = content[4 : 4 + msg_len].decode("utf-8")
            receive_id = content[4 + msg_len :].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WeChatCryptoError(f"解密结果不是合法 UTF-8，通常是 EncodingAESKey 不匹配：{exc}") from exc
        if self.appid and receive_id and receive_id != self.appid:
            raise WeChatCryptoError(
                f"AppID 校验失败：密文里是 {receive_id}，配置里是 {self.appid}"
            )
        return msg


def encrypt_echo(plaintext: str, token: str, encoding_aes_key: str, appid: str) -> str:
    """便捷函数：给测试用。"""
    return WeChatCrypto(token, encoding_aes_key, appid).encrypt(plaintext)
