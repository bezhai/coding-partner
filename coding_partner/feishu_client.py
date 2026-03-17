"""Feishu/Lark API client wrapper."""

import asyncio
import json
import logging
import threading
import uuid

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
    UpdateChatRequest,
    UpdateChatRequestBody,
)

from coding_partner.config import settings

logger = logging.getLogger(__name__)

_client: lark.Client | None = None
_client_lock = threading.Lock()


def get_client() -> lark.Client:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                log_level = (
                    lark.LogLevel.DEBUG if settings.log_level == "DEBUG" else lark.LogLevel.INFO
                )
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
        send_text(chat_id, f"[卡片发送失败] code={resp.code} {resp.msg}")
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
        reply_text(message_id, f"[卡片回复失败] code={resp.code} {resp.msg}")


def update_card(message_id: str, card: dict, *, chat_id: str | None = None) -> bool:
    """Update (patch) an existing card message. Returns True on success.

    If chat_id is provided and the update fails, a text error message is sent to the chat.
    """
    client = get_client()
    body = PatchMessageRequestBody.builder().content(json.dumps(card)).build()
    req = PatchMessageRequest.builder().message_id(message_id).request_body(body).build()
    resp = client.im.v1.message.patch(req)

    if not resp.success():
        logger.error("update_card failed: %s %s", resp.code, resp.msg)
        if chat_id:
            send_text(chat_id, f"[卡片更新失败] code={resp.code} {resp.msg}")
        return False
    return True


def download_image(image_key: str) -> bytes | None:
    """Download an image by image_key from Feishu (standalone image messages)."""
    from lark_oapi.api.im.v1 import GetImageRequest

    client = get_client()
    req = GetImageRequest.builder().image_key(image_key).build()
    resp = client.im.v1.image.get(req)

    if not resp.success():
        logger.error("download_image failed: %s %s", resp.code, resp.msg)
        return None
    return resp.file.read() if resp.file else None


def download_message_resource(message_id: str, file_key: str) -> bytes | None:
    """Download a resource (image/file) embedded in a message (e.g. rich text)."""
    from lark_oapi.api.im.v1 import GetMessageResourceRequest

    client = get_client()
    req = (
        GetMessageResourceRequest.builder()
        .message_id(message_id)
        .file_key(file_key)
        .type("image")
        .build()
    )
    resp = client.im.v1.message_resource.get(req)

    if not resp.success():
        logger.error("download_message_resource failed: %s %s", resp.code, resp.msg)
        return None
    return resp.file.read() if resp.file else None


def archive_chat(chat_id: str) -> None:
    """Archive (dissolve) a chat group."""
    client = get_client()
    body = UpdateChatRequestBody.builder().name("[已归档]").description("已完成").build()
    req = UpdateChatRequest.builder().chat_id(chat_id).request_body(body).build()
    resp = client.im.v1.chat.update(req)

    if not resp.success():
        logger.error("archive_chat failed: %s %s", resp.code, resp.msg)


# ---------------------------------------------------------------------------
# Async wrappers — run blocking Feishu SDK calls in a thread pool so they
# don't block the asyncio event loop.
# ---------------------------------------------------------------------------


async def async_reply_text(message_id: str, text: str) -> None:
    await asyncio.to_thread(reply_text, message_id, text)


async def async_send_text(chat_id: str, text: str) -> str | None:
    return await asyncio.to_thread(send_text, chat_id, text)


async def async_send_card(chat_id: str, card: dict) -> str | None:
    return await asyncio.to_thread(send_card, chat_id, card)


async def async_reply_card(message_id: str, card: dict) -> None:
    await asyncio.to_thread(reply_card, message_id, card)


async def async_update_card(message_id: str, card: dict, *, chat_id: str | None = None) -> bool:
    return await asyncio.to_thread(update_card, message_id, card, chat_id=chat_id)


async def async_download_image(image_key: str) -> bytes | None:
    return await asyncio.to_thread(download_image, image_key)


async def async_download_message_resource(message_id: str, file_key: str) -> bytes | None:
    return await asyncio.to_thread(download_message_resource, message_id, file_key)
