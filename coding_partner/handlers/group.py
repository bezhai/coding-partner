"""Group chat message handler.

Messages in dev groups are forwarded to Claude Code via a per-chat queue.
Commands: /new (reset session), /done (archive + cleanup), /cancel (stop current task).
"""

import asyncio
import logging
import time

from coding_partner import feishu_client, formatter, store
from coding_partner.config import settings
from coding_partner.services import claude_runner, worktree
from coding_partner.services.claude_runner import (
    WRITE_TOOLS,
    StreamDelta,
    StreamQuestion,
    StreamResult,
    StreamToolUse,
)

logger = logging.getLogger(__name__)

# Per-chat worker tasks
_workers: dict[str, asyncio.Task] = {}

# Per-chat running Claude process (for /cancel)
_running_procs: dict[str, asyncio.subprocess.Process] = {}

# Pending plan approvals: chat_id -> {session_id, cwd, plan_text}
_pending_plans: dict[str, dict] = {}


def ensure_worker(chat_id: str) -> None:
    """Ensure a queue-consuming worker is running for the given chat."""
    task = _workers.get(chat_id)
    if task is None or task.done():
        _workers[chat_id] = asyncio.create_task(_queue_worker(chat_id))


async def _queue_worker(chat_id: str) -> None:
    """Consume messages from the queue for a single chat, one at a time."""
    logger.info("Queue worker started for %s", chat_id)
    try:
        while True:
            msg = await store.dequeue_message(chat_id)
            if msg is None:
                # Queue empty — stop worker
                break

            binding = await store.get_binding(chat_id)
            if not binding:
                # Chat no longer managed, drain remaining messages
                await store.clear_queue(chat_id)
                break

            await store.delete_queued_message(msg.id)
            await _run_claude_streaming(msg.text, chat_id, binding)
    except Exception:
        logger.exception("Queue worker error for %s", chat_id)
    finally:
        _workers.pop(chat_id, None)
        logger.info("Queue worker stopped for %s", chat_id)


async def handle_group_message(
    message_id: str,
    user_open_id: str,
    text: str,
    chat_id: str,
) -> None:
    """Handle a group chat text message."""
    text = text.strip()

    try:
        binding = await store.get_binding(chat_id)
        if not binding:
            # Not a managed dev group, ignore
            return

        if text == "/new":
            await _handle_new(message_id, chat_id, binding)
        elif text == "/done":
            await _handle_done(message_id, chat_id, binding)
        elif text == "/cancel":
            await _handle_cancel(message_id, chat_id)
        elif text == "/confirm":
            await store.update_permission_mode(chat_id, "confirm")
            feishu_client.reply_text(
                message_id, "已切换为 confirm 模式 — Claude 修改代码前会先发方案给你确认"
            )
        elif text == "/auto":
            await store.update_permission_mode(chat_id, "auto")
            feishu_client.reply_text(
                message_id, "已切换为 auto 模式 — Claude 将自动执行所有操作"
            )
        else:
            # Enqueue and ensure worker is running
            await store.enqueue_message(chat_id, message_id, text)
            feishu_client.reply_text(message_id, "✓ 已排入队列")
            ensure_worker(chat_id)
    except Exception:
        logger.exception("Group handler error")


async def _handle_new(message_id: str, chat_id: str, binding: store.ChatBinding) -> None:
    """Reset Claude session context and clear message queue."""
    cleared = await store.clear_queue(chat_id)
    await store.update_session_id(chat_id, None)
    extra = f"（已清空队列中 {cleared} 条消息）" if cleared else ""
    feishu_client.reply_text(message_id, f"会话已重置，后续消息将开始新的 Claude 上下文{extra}")


async def _handle_cancel(message_id: str, chat_id: str) -> None:
    """Kill the running Claude process and clear the queue."""
    # Kill running process
    proc = _running_procs.pop(chat_id, None)
    if proc and proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass

    # Cancel worker task
    task = _workers.pop(chat_id, None)
    if task and not task.done():
        task.cancel()

    # Clear pending queue
    cleared = await store.clear_queue(chat_id)
    parts = ["已取消当前任务"]
    if cleared:
        parts.append(f"清空队列中 {cleared} 条消息")
    feishu_client.reply_text(message_id, "，".join(parts))


async def _handle_done(message_id: str, chat_id: str, binding: store.ChatBinding) -> None:
    """Clean up: cancel running task, clear queue, remove worktree, delete group."""
    # Cancel running process
    proc = _running_procs.pop(chat_id, None)
    if proc and proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass

    # Cancel worker
    task = _workers.pop(chat_id, None)
    if task and not task.done():
        task.cancel()

    await store.clear_queue(chat_id)
    feishu_client.reply_text(message_id, "正在清理...")

    # Only clean up worktree if it's different from repo (i.e. not direct mode)
    if binding.worktree_path != binding.repo_path:
        try:
            await worktree.cleanup_worktree(binding.worktree_path, binding.repo_path)
        except Exception as e:
            logger.warning("Worktree cleanup failed: %s", e)

    from coding_partner.services.group_manager import delete_group

    try:
        delete_group(chat_id)
    except Exception as e:
        logger.warning("Delete group failed: %s", e)

    await store.delete_binding(chat_id)


async def _run_claude_streaming(
    text: str,
    chat_id: str,
    binding: store.ChatBinding,
    *,
    disallowed_tools: list[str] | None = None,
) -> None:
    """Run Claude with streaming, updating the card periodically.

    In 'confirm' permission mode, the first pass runs read-only (write tools blocked).
    The plan result is sent as an approval card. On approval, a second pass resumes
    with all tools enabled.
    """
    is_confirm_mode = (
        binding.permission_mode == "confirm" and disallowed_tools is None
    )
    effective_disallowed = WRITE_TOOLS if is_confirm_mode else disallowed_tools

    # Send initial thinking card
    label = "Claude 正在分析中..." if is_confirm_mode else "Claude 正在工作中..."
    thinking_card = formatter.build_thinking_card(text)
    card_msg_id = feishu_client.send_card(chat_id, thinking_card)

    accumulated = ""
    tool_activities: list[str] = []
    last_update_time = time.monotonic()
    need_update = False

    def _track_proc(proc: asyncio.subprocess.Process) -> None:
        _running_procs[chat_id] = proc

    async for event in claude_runner.run_stream(
        prompt=text,
        cwd=binding.worktree_path,
        session_id=binding.session_id,
        proc_callback=_track_proc,
        disallowed_tools=effective_disallowed,
    ):
        if isinstance(event, StreamDelta):
            accumulated += event.text
            need_update = True

        elif isinstance(event, StreamToolUse):
            tool_activities.append(event.summary)
            if len(tool_activities) > settings.tool_activity_limit:
                tool_activities = tool_activities[-settings.tool_activity_limit :]
            need_update = True

        elif isinstance(event, StreamQuestion):
            question_card = formatter.build_question_card(
                event.question, event.options, chat_id
            )
            feishu_client.send_card(chat_id, question_card)

        elif isinstance(event, StreamResult):
            _running_procs.pop(chat_id, None)
            result = event.result

            if result.session_id:
                await store.update_session_id(chat_id, result.session_id)

            # In confirm mode: show plan approval card instead of result
            if is_confirm_mode and not result.is_error:
                _pending_plans[chat_id] = {
                    "session_id": result.session_id,
                    "cwd": binding.worktree_path,
                    "plan_text": result.result,
                }
                plan_card = formatter.build_plan_approval_card(result.result, chat_id)
                if card_msg_id:
                    feishu_client.update_card(card_msg_id, plan_card)
                else:
                    feishu_client.send_card(chat_id, plan_card)
            else:
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

        # Cooldown-based card update for progress events
        if need_update:
            now = time.monotonic()
            if now - last_update_time >= settings.stream_cooldown:
                if card_msg_id:
                    streaming_card = formatter.build_streaming_card(
                        text, accumulated, tool_activities
                    )
                    feishu_client.update_card(card_msg_id, streaming_card)
                last_update_time = now
                need_update = False


async def handle_plan_approval(chat_id: str, approved: bool) -> None:
    """Handle plan approval callback — run second pass with all tools if approved."""
    pending = _pending_plans.pop(chat_id, None)
    if not pending:
        feishu_client.send_text(chat_id, "该方案已过期，请重新发送需求")
        return

    if not approved:
        feishu_client.send_text(chat_id, "已拒绝方案，你可以继续发送修改后的需求")
        return

    binding = await store.get_binding(chat_id)
    if not binding:
        feishu_client.send_text(chat_id, "开发群已失效")
        return

    # Second pass: resume session with all tools enabled
    feishu_client.send_text(chat_id, "方案已批准，开始执行...")
    await _run_claude_streaming(
        "用户已批准方案，请继续执行",
        chat_id,
        binding,
        disallowed_tools=[],  # explicitly empty = all tools allowed
    )
