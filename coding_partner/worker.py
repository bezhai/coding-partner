"""Worker process entry point.

Usage: python -m coding_partner.worker <chat_id>

Consumes messages from the SQLite queue for a single chat, runs the agent,
updates Feishu cards, and exits when the queue is empty or SIGTERM is received.
"""

import asyncio
import json
import logging
import signal
import sys
import time

from coding_partner import feishu_client, formatter, store
from coding_partner.config import settings
from coding_partner.services.agent_runner import (
    StreamDelta,
    StreamQuestion,
    StreamResult,
    StreamToolUse,
    provider_display_name,
    run_stream,
    write_tools_for_provider,
)

logger = logging.getLogger(__name__)

# Module-level state for signal handler access
_shutdown_event = asyncio.Event()
_proc: asyncio.subprocess.Process | None = None
_current_card_msg_id: str | None = None
_current_chat_id: str | None = None


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """Install SIGTERM handler that triggers graceful shutdown."""

    def _handle_sigterm():
        logger.info("Worker received SIGTERM, shutting down...")
        _shutdown_event.set()
        # Kill current agent subprocess if running
        if _proc is not None and _proc.returncode is None:
            try:
                _proc.kill()
            except ProcessLookupError:
                pass

    loop.add_signal_handler(signal.SIGTERM, _handle_sigterm)


async def _run_agent_streaming(
    msg: store.QueuedMessage,
    chat_id: str,
    binding: store.ChatBinding,
) -> None:
    """Run the configured agent with streaming, updating the card periodically."""
    global _proc, _current_card_msg_id

    image_paths = [p for p in msg.image_paths.split(",") if p] if msg.image_paths else []

    # Determine disallowed_tools from message or binding
    if msg.disallowed_tools == "[]":
        # Explicitly allow all tools (plan approval second pass)
        disallowed_tools: list[str] | None = []
    elif msg.disallowed_tools:
        disallowed_tools = json.loads(msg.disallowed_tools)
    else:
        disallowed_tools = None  # use binding default

    agent_name = provider_display_name(binding.agent_provider)
    is_confirm_mode = binding.permission_mode == "confirm" and disallowed_tools is None
    effective_disallowed = (
        write_tools_for_provider(binding.agent_provider) if is_confirm_mode else disallowed_tools
    )

    text = msg.text

    # Send thinking card
    thinking_card = formatter.build_thinking_card(text, agent_name=agent_name)
    card_msg_id = await feishu_client.async_send_card(chat_id, thinking_card)

    if card_msg_id:
        _current_card_msg_id = card_msg_id
        # Persist active card to DB for crash recovery
        await store.save_active_cards({chat_id: card_msg_id})

    accumulated = ""
    tool_activities: list[str] = []
    last_update_time = time.monotonic()
    need_update = False
    completed = False
    last_was_tool = False
    entered_plan_mode = False

    def _track_proc(proc: asyncio.subprocess.Process) -> None:
        global _proc
        _proc = proc

    try:
        async for event in run_stream(
            prompt=text,
            cwd=binding.worktree_path,
            session_id=binding.session_id,
            proc_callback=_track_proc,
            disallowed_tools=effective_disallowed,
            image_paths=image_paths,
            provider=binding.agent_provider,
        ):
            if isinstance(event, StreamDelta):
                if last_was_tool and accumulated:
                    accumulated += "\n\n"
                accumulated += event.text
                last_was_tool = False
                need_update = True

            elif isinstance(event, StreamToolUse):
                if event.name == "EnterPlanMode":
                    entered_plan_mode = True
                tool_activities.append(event.summary)
                if len(tool_activities) > settings.tool_activity_limit:
                    tool_activities = tool_activities[-settings.tool_activity_limit :]
                last_was_tool = True
                need_update = True

            elif isinstance(event, StreamQuestion):
                question_card = formatter.build_question_card(
                    event.question,
                    event.options,
                    chat_id,
                    agent_name=agent_name,
                    all_questions=event.all_questions,
                )
                await feishu_client.async_send_card(chat_id, question_card)

            elif isinstance(event, StreamResult):
                completed = True
                _proc = None
                result = event.result

                if result.session_id:
                    await store.update_session_id(chat_id, result.session_id)

                display_text = accumulated.strip() or result.result

                # In confirm mode or Claude entered plan mode: save plan and show approval card
                if (is_confirm_mode or entered_plan_mode) and not result.is_error:
                    await store.save_pending_plan(chat_id, result.session_id, display_text)
                    plan_card = formatter.build_plan_approval_card(
                        display_text,
                        chat_id,
                        agent_name=agent_name,
                    )
                    if not card_msg_id or not await feishu_client.async_update_card(card_msg_id, plan_card, chat_id=chat_id):
                        await feishu_client.async_send_card(chat_id, plan_card)
                else:
                    result_card = formatter.build_result_card(
                        display_text,
                        cost=result.cost,
                        duration=result.duration,
                        is_error=result.is_error,
                    )
                    if not card_msg_id or not await feishu_client.async_update_card(card_msg_id, result_card, chat_id=chat_id):
                        await feishu_client.async_send_card(chat_id, result_card)

            # Cooldown-based card update for progress events
            if need_update:
                now = time.monotonic()
                if now - last_update_time >= settings.stream_cooldown:
                    if card_msg_id:
                        streaming_card = formatter.build_streaming_card(
                            text,
                            accumulated,
                            tool_activities,
                            agent_name=agent_name,
                        )
                        await feishu_client.async_update_card(card_msg_id, streaming_card)
                    last_update_time = now
                    need_update = False
    finally:
        # Clear active card from DB
        await store.save_active_cards({})
        _current_card_msg_id = None
        _proc = None
        if not completed and card_msg_id:
            try:
                if _shutdown_event.is_set():
                    msg_text = "任务被中断"
                else:
                    msg_text = "任务被中断，请重新发送消息继续。"
                card = formatter.build_result_card(msg_text, is_error=True)
                await feishu_client.async_update_card(card_msg_id, card, chat_id=chat_id)
            except Exception:
                pass


async def main(chat_id: str) -> None:
    """Main worker loop: consume messages from the queue until empty or shutdown."""
    global _current_chat_id
    _current_chat_id = chat_id

    await store.get_db()
    loop = asyncio.get_event_loop()
    _install_signal_handlers(loop)

    logger.info("Worker started for chat %s (pid=%d)", chat_id, __import__("os").getpid())

    try:
        while not _shutdown_event.is_set():
            msg = await store.dequeue_message(chat_id)
            if msg is None:
                break  # queue empty

            binding = await store.get_binding(chat_id)
            if not binding:
                await store.clear_queue(chat_id)
                break

            await store.delete_queued_message(msg.id)
            await _run_agent_streaming(msg, chat_id, binding)
    except Exception:
        logger.exception("Worker error for chat %s", chat_id)
    finally:
        await store.close_db()
        logger.info("Worker exiting for chat %s", chat_id)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python -m coding_partner.worker <chat_id>", file=sys.stderr)
        sys.exit(1)

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    asyncio.run(main(sys.argv[1]))
