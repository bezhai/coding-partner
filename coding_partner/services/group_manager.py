"""Feishu group (chat) creation and management."""

import logging

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateChatMembersRequest,
    CreateChatMembersRequestBody,
    CreateChatRequest,
    CreateChatRequestBody,
    DeleteChatRequest,
)

from coding_partner.config import settings
from coding_partner.feishu_client import get_client

logger = logging.getLogger(__name__)


def create_dev_group(user_open_id: str, branch_name: str, repo_name: str) -> str | None:
    """Create a dev group chat, add user, return chat_id."""
    client = get_client()

    group_name = f"{settings.normalized_group_name_prefix}🔧 {repo_name} | {branch_name}"

    body = (
        CreateChatRequestBody.builder()
        .name(group_name)
        .description(f"Coding Partner: {repo_name} / {branch_name}")
        .chat_mode("group")
        .chat_type("private")
        .build()
    )

    req = CreateChatRequest.builder().request_body(body).build()
    resp = client.im.v1.chat.create(req)

    if not resp.success():
        logger.error("create_dev_group failed: %s %s", resp.code, resp.msg)
        return None

    chat_id = resp.data.chat_id
    logger.info("Created group: %s (%s)", group_name, chat_id)

    # Add user to the group
    if user_open_id:
        _add_member(client, chat_id, user_open_id)

    return chat_id


def _add_member(client: lark.Client, chat_id: str, open_id: str) -> None:
    body = CreateChatMembersRequestBody.builder().id_list([open_id]).build()
    req = (
        CreateChatMembersRequest.builder()
        .chat_id(chat_id)
        .member_id_type("open_id")
        .request_body(body)
        .build()
    )
    resp = client.im.v1.chat_members.create(req)
    if not resp.success():
        logger.error("add_member failed: %s %s", resp.code, resp.msg)


def delete_group(chat_id: str) -> None:
    """Delete (dissolve) a group chat."""
    client = get_client()
    req = DeleteChatRequest.builder().chat_id(chat_id).build()
    resp = client.im.v1.chat.delete(req)

    if not resp.success():
        logger.error("delete_group failed: %s %s", resp.code, resp.msg)
    else:
        logger.info("Deleted group: %s", chat_id)
