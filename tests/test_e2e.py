"""端到端测试：直接模拟微信服务器，把整条链路跑一遍。"""

from __future__ import annotations

import time
import uuid
import xml.etree.ElementTree as ET

import pytest
from conftest import AES_KEY, APPID, TOKEN
from fastapi.testclient import TestClient

from app.main import app
from app.wechat.crypto import WeChatCrypto
from app.wechat.xml import parse_message

crypto = WeChatCrypto(TOKEN, AES_KEY, APPID)


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client


def unique_openid() -> str:
    return "oUser" + uuid.uuid4().hex[:12]


def text_xml(openid: str, content: str, msg_id: str, gh_id: str = "gh_test") -> str:
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{gh_id}]]></ToUserName>"
        f"<FromUserName><![CDATA[{openid}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        f"<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>"
        f"<MsgId>{msg_id}</MsgId>"
        "</xml>"
    )


def event_xml(openid: str, event: str, gh_id: str = "gh_test") -> str:
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{gh_id}]]></ToUserName>"
        f"<FromUserName><![CDATA[{openid}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        f"<MsgType><![CDATA[event]]></MsgType>"
        f"<Event><![CDATA[{event}]]></Event>"
        "</xml>"
    )


def image_xml(openid: str, gh_id: str = "gh_test") -> str:
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{gh_id}]]></ToUserName>"
        f"<FromUserName><![CDATA[{openid}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        f"<MsgType><![CDATA[image]]></MsgType>"
        "<PicUrl><![CDATA[http://example.com/a.jpg]]></PicUrl>"
        "</xml>"
    )


def post_plain(client: TestClient, openid: str, content: str, msg_id: str | None = None):
    ts, nonce = str(int(time.time())), uuid.uuid4().hex[:10]
    body = text_xml(openid, content, msg_id or str(uuid.uuid4().int))
    return client.post(
        "/wechat",
        params={
            "signature": crypto.sign(ts, nonce),
            "timestamp": ts,
            "nonce": nonce,
            "openid": openid,
        },
        content=body.encode("utf-8"),
        headers={"Content-Type": "text/xml"},
    )


def post_secure(client: TestClient, openid: str, content: str, msg_id: str | None = None):
    ts, nonce = str(int(time.time())), uuid.uuid4().hex[:10]
    encrypt = crypto.encrypt(text_xml(openid, content, msg_id or str(uuid.uuid4().int)))
    body = f"<xml><ToUserName><![CDATA[gh_test]]></ToUserName><Encrypt><![CDATA[{encrypt}]]></Encrypt></xml>"
    return client.post(
        "/wechat",
        params={
            "signature": crypto.sign(ts, nonce),
            "timestamp": ts,
            "nonce": nonce,
            "openid": openid,
            "encrypt_type": "aes",
            "msg_signature": crypto.msg_signature(ts, nonce, encrypt),
        },
        content=body.encode("utf-8"),
        headers={"Content-Type": "text/xml"},
    )


# ------------------------------------------------------------------ URL 校验
def test_url_verify_success(client: TestClient):
    ts, nonce, echostr = str(int(time.time())), uuid.uuid4().hex[:10], uuid.uuid4().hex
    resp = client.get(
        "/wechat",
        params={
            "signature": crypto.sign(ts, nonce),
            "timestamp": ts,
            "nonce": nonce,
            "echostr": echostr,
        },
    )
    assert resp.status_code == 200
    assert resp.text == echostr


def test_url_verify_bad_signature(client: TestClient):
    resp = client.get(
        "/wechat",
        params={"signature": "bad", "timestamp": "1", "nonce": "2", "echostr": "hello"},
    )
    assert resp.status_code == 403


# ------------------------------------------------------------------ 明文消息
def test_plaintext_text_message(client: TestClient):
    openid = unique_openid()
    resp = post_plain(client, openid, "你好，介绍一下你自己")
    assert resp.status_code == 200
    reply = parse_message(resp.content)
    assert reply["MsgType"] == "text"
    assert reply["ToUserName"] == openid
    assert reply["FromUserName"] == "gh_test"
    assert "MOCK" in reply["Content"]
    assert "介绍一下你自己" in reply["Content"]


def test_duplicate_msg_id_is_ignored(client: TestClient):
    openid = unique_openid()
    msg_id = str(uuid.uuid4().int)
    first = post_plain(client, openid, "同一条消息", msg_id)
    second = post_plain(client, openid, "同一条消息", msg_id)
    assert first.status_code == 200 and "<xml>" in first.text
    assert second.status_code == 200 and second.text.strip() == "success"


def test_history_is_stored(client: TestClient):
    openid = unique_openid()
    post_plain(client, openid, "第一句")
    time.sleep(0.05)
    post_plain(client, openid, "第二句")
    history = app.state.store.history(openid)
    roles = [item["role"] for item in history]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert history[0]["content"] == "第一句"
    assert history[2]["content"] == "第二句"


# ------------------------------------------------------------------ 安全模式
def test_safe_mode_message_roundtrip(client: TestClient):
    openid = unique_openid()
    resp = post_secure(client, openid, "安全模式下的问题")
    assert resp.status_code == 200
    outer = parse_message(resp.content)
    assert "Encrypt" in outer
    # 服务端返回的密文签名必须能被微信校验通过
    assert crypto.check_msg_signature(
        outer["MsgSignature"], str(outer["TimeStamp"]), outer["Nonce"], outer["Encrypt"]
    )
    inner = parse_message(crypto.decrypt(outer["Encrypt"]))
    assert inner["ToUserName"] == openid
    assert "MOCK" in inner["Content"]
    assert "安全模式下的问题" in inner["Content"]


def test_safe_mode_bad_msg_signature(client: TestClient):
    openid = unique_openid()
    ts, nonce = str(int(time.time())), uuid.uuid4().hex[:10]
    encrypt = crypto.encrypt(text_xml(openid, "x", str(uuid.uuid4().int)))
    body = f"<xml><Encrypt><![CDATA[{encrypt}]]></Encrypt></xml>"
    resp = client.post(
        "/wechat",
        params={
            "timestamp": ts,
            "nonce": nonce,
            "encrypt_type": "aes",
            "msg_signature": "deadbeef",
        },
        content=body.encode("utf-8"),
    )
    assert resp.status_code == 403


# ------------------------------------------------------------------ 事件与其它类型
def test_subscribe_event_returns_welcome(client: TestClient):
    openid = unique_openid()
    ts, nonce = str(int(time.time())), uuid.uuid4().hex[:10]
    body = event_xml(openid, "subscribe")
    resp = client.post(
        "/wechat",
        params={"signature": crypto.sign(ts, nonce), "timestamp": ts, "nonce": nonce},
        content=body.encode("utf-8"),
    )
    assert resp.status_code == 200
    reply = parse_message(resp.content)
    assert "AI 助手" in reply["Content"]


def test_unsubscribe_event_returns_success(client: TestClient):
    openid = unique_openid()
    ts, nonce = str(int(time.time())), uuid.uuid4().hex[:10]
    resp = client.post(
        "/wechat",
        params={"signature": crypto.sign(ts, nonce), "timestamp": ts, "nonce": nonce},
        content=event_xml(openid, "unsubscribe").encode("utf-8"),
    )
    assert resp.status_code == 200
    assert resp.text.strip() == "success"


def test_image_message_gets_hint(client: TestClient):
    openid = unique_openid()
    ts, nonce = str(int(time.time())), uuid.uuid4().hex[:10]
    resp = client.post(
        "/wechat",
        params={"signature": crypto.sign(ts, nonce), "timestamp": ts, "nonce": nonce},
        content=image_xml(openid).encode("utf-8"),
    )
    reply = parse_message(resp.content)
    assert "文字" in reply["Content"]


def test_broken_xml_does_not_crash(client: TestClient):
    ts, nonce = str(int(time.time())), uuid.uuid4().hex[:10]
    resp = client.post(
        "/wechat",
        params={"signature": crypto.sign(ts, nonce), "timestamp": ts, "nonce": nonce},
        content=b"<xml>not-closed",
    )
    assert resp.status_code == 200
    assert resp.text.strip() == "success"


# ------------------------------------------------------------------ 其它路由
def test_healthz(client: TestClient):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["safe_mode"] is True
    assert data["push_enabled"] is False  # 测试环境没有 AppSecret


def test_index(client: TestClient):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["service"] == "wechat-deepseek"
