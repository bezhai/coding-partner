"""Group chat message handler.

Messages in dev groups are forwarded to the configured coding agent via a per-chat queue.
Commands: /new (reset session), /done (archive + cleanup), /cancel (stop current task).

This module is now a thin routing layer — agent execution happens in Worker subprocesses
managed by worker_manager.
"""

import asyncio
import logging
import uuid
from pathlib import Path

from coding_partner import feishu_client, store, worker_manager
from coding_partner.config import settings
from coding_partner.services import worktree
from coding_partner.services.agent_runner import provider_display_name

logger = logging.getLogger(__name__)


def _save_image_to_worktree(image_data: bytes, worktree_path: str) -> str:
    """Save image data to the worktree and return the file path."""
    img_dir = Path(worktree_path) / ".coding-partner" / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex[:12]}.png"
    img_path = img_dir / filename
    img_path.write_bytes(image_data)
    return str(img_path)


async def handle_group_message(
    message_id: str,
    user_open_id: str,
    text: str | None,
    chat_id: str,
    image_key: str | None = None,
) -> None:
    """Handle a group chat message (text and/or image)."""
    if text:
        text = text.strip()

    try:
        binding = await store.get_binding(chat_id)
        if not binding:
            if text and text in ("/done", "/new", "/cancel"):
                await feishu_client.async_reply_text(message_id, "当前群未绑定开发环境")
            return

        # Commands are text-only
        if text == "/new":
            await _handle_new(message_id, chat_id, binding)
        elif text == "/done":
            await _handle_done(message_id, chat_id, binding)
        elif text == "/cancel":
            await _handle_cancel(message_id, chat_id)
        elif text == "/confirm":
            await store.update_permission_mode(chat_id, "confirm")
            agent_name = provider_display_name(binding.agent_provider)
            await feishu_client.async_reply_text(
                message_id,
                f"已切换为 confirm 模式 — {agent_name} 修改代码前会先发方案给你确认",
            )
        elif text == "/auto":
            await store.update_permission_mode(chat_id, "auto")
            agent_name = provider_display_name(binding.agent_provider)
            await feishu_client.async_reply_text(
                message_id,
                f"已切换为 auto 模式 — {agent_name} 将自动执行所有操作",
            )
        else:
            # Handle image: download and save to worktree
            saved_image_path = ""
            if image_key:
                image_data = await feishu_client.async_download_image(image_key)
                if not image_data:
                    # Fallback: image embedded in rich text (post) is a message resource
                    image_data = await feishu_client.async_download_message_resource(
                        message_id, image_key
                    )
                if image_data:
                    saved_image_path = _save_image_to_worktree(
                        image_data, binding.worktree_path
                    )
                else:
                    await feishu_client.async_reply_text(message_id, "图片下载失败")
                    return

            prompt = text or ""
            if not prompt and saved_image_path:
                prompt = "请查看用户发送的图片并回应"

            # Enqueue and ensure worker subprocess is running
            await store.enqueue_message(chat_id, message_id, prompt, saved_image_path)
            await feishu_client.async_reply_text(message_id, "✓ 已排入队列")
            await worker_manager.ensure_worker(chat_id)
    except Exception:
        logger.exception("Group handler error")


async def _handle_new(message_id: str, chat_id: str, binding: store.ChatBinding) -> None:
    """Reset agent session context and clear message queue."""
    await worker_manager.kill_worker(chat_id)
    cleared = await store.clear_queue(chat_id)
    await store.update_session_id(chat_id, None)
    await store.delete_pending_plan(chat_id)
    extra = f"（已清空队列中 {cleared} 条消息）" if cleared else ""
    agent_name = provider_display_name(binding.agent_provider)
    await feishu_client.async_reply_text(
        message_id,
        f"会话已重置，后续消息将开始新的 {agent_name} 上下文{extra}",
    )


async def _handle_cancel(message_id: str, chat_id: str) -> None:
    """Kill the running worker and clear the queue."""
    await worker_manager.kill_worker(chat_id)
    await worker_manager.wait_worker(chat_id)
    cleared = await store.clear_queue(chat_id)
    parts = ["已取消当前任务"]
    if cleared:
        parts.append(f"清空队列中 {cleared} 条消息")
    await feishu_client.async_reply_text(message_id, "，".join(parts))


async def _handle_done(message_id: str, chat_id: str, binding: store.ChatBinding) -> None:
    """Clean up: kill worker, clear queue, remove worktree, delete group."""
    await worker_manager.kill_worker(chat_id)
    await worker_manager.wait_worker(chat_id)

    await store.clear_queue(chat_id)
    await store.delete_pending_plan(chat_id)
    await feishu_client.async_reply_text(message_id, "正在清理...")

    # Only clean up worktree if it's different from repo (i.e. not direct mode)
    if binding.worktree_path != binding.repo_path:
        try:
            await worktree.cleanup_worktree(
                binding.worktree_path, binding.repo_path, binding.branch_name
            )
        except Exception as e:
            logger.warning("Worktree cleanup failed: %s", e)

    from coding_partner.services.group_manager import delete_group

    try:
        await asyncio.to_thread(delete_group, chat_id)
    except Exception as e:
        logger.warning("Delete group failed: %s", e)

    await store.delete_binding(chat_id)


async def handle_plan_approval(chat_id: str, approved: bool) -> None:
    """Handle plan approval callback — enqueue second pass with all tools if approved."""
    pending = await store.get_pending_plan(chat_id)
    if not pending:
        await feishu_client.async_send_text(chat_id, "该方案已过期，请重新发送需求")
        return

    await store.delete_pending_plan(chat_id)

    if not approved:
        await feishu_client.async_send_text(chat_id, "已拒绝方案，你可以继续发送修改后的需求")
        return

    binding = await store.get_binding(chat_id)
    if not binding:
        await feishu_client.async_send_text(chat_id, "开发群已失效")
        return

    # Second pass: enqueue with all tools explicitly allowed
    await feishu_client.async_send_text(chat_id, "方案已批准，开始执行...")
    await store.enqueue_message(
        chat_id, "", "用户已批准方案，请继续执行",
        disallowed_tools="[]",  # explicitly empty = all tools allowed
    )
    await worker_manager.ensure_worker(chat_id)
