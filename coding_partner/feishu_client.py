"""Feishu/Lark API client wrapper."""

import json
import logging
import uuid

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchChatRequest,
    PatchChatRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from coding_partner.config import settings

logger = logging.getLogger(__name__)

_client: lark.Client | None = None


def get_client() -> lark.Client:
    global _client
    if _client is None:
        log_level = lark.LogLevel.DEBUG if settings.log_level == "DEBUG" else lark.LogLevel.INFO
        _client = (
            lark.Client.builder()
            .app_id(settings.feishu_app_id)
            .app_secret(settings.feishu_app_secret)
            .log_level(log_level)
            .build()
        )
    return _client


def reply_text(message_id: str, text: str) -> None:
    """Reply to a message with plain text."""
    client = get_client()
    body = (
        ReplyMessageRequestBody.builder()
        .content(json.dumps({"text": text}))
        .msg_type("text")
        .uuid(str(uuid.uuid4()))
        .build()
    )

    req = ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
    resp = client.im.v1.message.reply(req)

    if not resp.success():
        logger.error("reply_text failed: %s %s", resp.code, resp.msg)


def send_text(chat_id: str, text: str) -> str | None:
    """Send a text message to a chat, return message_id."""
    client = get_client()
    body = (
        CreateMessageRequestBody.builder()
        .receive_id(chat_id)
        .content(json.dumps({"text": text}))
        .msg_type("text")
        .uuid(str(uuid.uuid4()))
        .build()
    )

    req = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
    resp = client.im.v1.message.create(req)

    if not resp.success():
        logger.error("send_text failed: %s %s", resp.code, resp.msg)
        return None
    return resp.data.message_id if resp.data else None


def send_card(chat_id: str, card: dict) -> str | None:
    """Send an interactive card to a chat, return message_id."""
    client = get_client()
    body = (
        CreateMessageRequestBody.builder()
        .receive_id(chat_id)
        .content(json.dumps(card))
        .msg_type("interactive")
        .uuid(str(uuid.uuid4()))
        .build()
    )

    req = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
    resp = client.im.v1.message.create(req)

    if not resp.success():
        logger.error("send_card failed: %s %s", resp.code, resp.msg)
        return None
    return resp.data.message_id if resp.data else None


def reply_card(message_id: str, card: dict) -> None:
    """Reply to a message with an interactive card."""
    client = get_client()
    body = (
        ReplyMessageRequestBody.builder()
        .content(json.dumps(card))
        .msg_type("interactive")
        .uuid(str(uuid.uuid4()))
        .build()
    )

    req = ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
    resp = client.im.v1.message.reply(req)

    if not resp.success():
        logger.error("reply_card failed: %s %s", resp.code, resp.msg)


def update_card(message_id: str, card: dict) -> None:
    """Update (patch) an existing card message."""
    client = get_client()
    req = lark.RawRequest()
    req.uri = f"/open-apis/im/v1/messages/{message_id}"
    req.http_method = "PATCH"
    req.body = {"content": json.dumps(card)}
    resp = client.request(req)

    if resp.code != 0:
        logger.error("update_card failed: %s %s", resp.code, resp.msg)


def archive_chat(chat_id: str) -> None:
    """Archive (dissolve) a chat group."""
    client = get_client()
    body = PatchChatRequestBody.builder().name("[已归档]").description("已完成").build()
    req = PatchChatRequest.builder().chat_id(chat_id).request_body(body).build()
    resp = client.im.v1.chat.patch(req)

    if not resp.success():
        logger.error("archive_chat failed: %s %s", resp.code, resp.msg)
