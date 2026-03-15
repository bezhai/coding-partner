"""Group chat message handler.

Messages in dev groups are forwarded to Claude Code.
Commands: /new (reset session), /done (archive + cleanup).
"""

import asyncio
import logging

from coding_partner import feishu_client, formatter, store
from coding_partner.services import claude_runner, worktree

logger = logging.getLogger(__name__)

# Per-chat locks to prevent concurrent Claude runs in the same group
_chat_locks: dict[str, asyncio.Lock] = {}


def _get_lock(chat_id: str) -> asyncio.Lock:
    if chat_id not in _chat_locks:
        _chat_locks[chat_id] = asyncio.Lock()
    return _chat_locks[chat_id]


async def handle_group_message(
    message_id: str,
    user_open_id: str,
    text: str,
    chat_id: str,
) -> None:
    """Handle a group chat text message."""
    text = text.strip()

    binding = await store.get_binding(chat_id)
    if not binding:
        # Not a managed dev group, ignore
        return

    if text == "/new":
        await _handle_new(message_id, chat_id, binding)
    elif text == "/done":
        await _handle_done(message_id, chat_id, binding)
    else:
        await _handle_claude_message(message_id, text, chat_id, binding)


async def _handle_new(message_id: str, chat_id: str, binding: store.ChatBinding) -> None:
    """Reset Claude session context."""
    await store.update_session_id(chat_id, None)
    feishu_client.reply_text(message_id, "会话已重置，后续消息将开始新的 Claude 上下文")


async def _handle_done(message_id: str, chat_id: str, binding: store.ChatBinding) -> None:
    """Commit changes, archive group, cleanup worktree."""
    feishu_client.reply_text(message_id, "正在收尾...")

    try:
        # Ask Claude to commit
        commit_result = await claude_runner.run(
            prompt=(
                "请检查当前工作目录的改动，如果有未提交的改动就用 git 提交"
                "（commit message 说明做了什么），然后输出 git log --oneline -5 的结果"
            ),
            cwd=binding.worktree_path,
            session_id=binding.session_id,
        )

        result_card = formatter.build_result_card(
            f"**收尾完成**\n\n{commit_result.result}",
            duration=commit_result.duration,
            is_error=commit_result.is_error,
        )
        feishu_client.send_card(chat_id, result_card)

    except Exception as e:
        logger.exception("Commit before done failed")
        feishu_client.send_text(chat_id, f"提交失败: {e}")

    try:
        # Cleanup worktree
        await worktree.cleanup_worktree(binding.worktree_path, binding.repo_path)
    except Exception as e:
        logger.warning("Worktree cleanup failed: %s", e)

    # Archive the group
    feishu_client.archive_chat(chat_id)

    # Clean up binding and lock
    await store.delete_binding(chat_id)
    _chat_locks.pop(chat_id, None)


async def _handle_claude_message(
    message_id: str,
    text: str,
    chat_id: str,
    binding: store.ChatBinding,
) -> None:
    """Forward message to Claude Code and return result."""
    lock = _get_lock(chat_id)

    if lock.locked():
        feishu_client.reply_text(message_id, "Claude 正在执行上一个任务，请稍候...")
        return

    async with lock:
        # Send thinking card
        thinking_card = formatter.build_thinking_card(text)
        card_msg_id = feishu_client.send_card(chat_id, thinking_card)

        # Run Claude
        result = await claude_runner.run(
            prompt=text,
            cwd=binding.worktree_path,
            session_id=binding.session_id,
        )

        # Update session
        if result.session_id:
            await store.update_session_id(chat_id, result.session_id)

        # Replace thinking card with result
        result_card = formatter.build_result_card(
            result.result,
            cost=result.cost,
            duration=result.duration,
            is_error=result.is_error,
        )
        if card_msg_id:
            feishu_client.update_card(card_msg_id, result_card)
        else:
            feishu_client.send_card(chat_id, result_card)
