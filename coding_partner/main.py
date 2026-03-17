"""Entry point: Feishu WebSocket bot with event routing (Gateway process)."""

import asyncio
import json
import logging
import re
import shutil
import signal

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from coding_partner import feishu_client, formatter, store, worker_manager
from coding_partner.config import settings
from coding_partner.handlers import dm, group

logger = logging.getLogger(__name__)

# asyncio event loop reference for scheduling coroutines from sync callbacks
_loop: asyncio.AbstractEventLoop | None = None

# Pattern to strip @mention placeholders like @_user_1
_MENTION_RE = re.compile(r"@_user_\d+\s*")


def _extract_content(message: dict) -> tuple[str | None, str | None]:
    """Extract text and/or image_key from a message.

    Returns (text, image_key). At least one will be non-None for valid messages.
    """
    msg_type = message.get("message_type", "")
    content_str = message.get("content", "{}")

    try:
        content = json.loads(content_str)
    except (json.JSONDecodeError, AttributeError):
        return None, None

    if msg_type == "text":
        text = content.get("text", "").strip()
        text = _MENTION_RE.sub("", text).strip()
        return (text if text else None), None

    if msg_type == "image":
        image_key = content.get("image_key", "")
        return None, (image_key if image_key else None)

    if msg_type == "post":
        return _extract_post_content(content)

    return None, None


def _extract_post_content(content: dict) -> tuple[str | None, str | None]:
    """Extract text and first image_key from a rich-text (post) message."""
    texts: list[str] = []
    first_image_key: str | None = None

    title = content.get("title", "")
    if title:
        texts.append(title)

    for para in content.get("content", []):
        for elem in para:
            tag = elem.get("tag", "")
            if tag == "text":
                texts.append(elem.get("text", ""))
            elif tag == "a":
                texts.append(elem.get("text", ""))
            elif tag == "img" and not first_image_key:
                first_image_key = elem.get("image_key") or None

    text = " ".join(t for t in texts if t).strip()
    text = _MENTION_RE.sub("", text).strip()
    return (text if text else None), first_image_key


def _is_bot_message(event: P2ImMessageReceiveV1) -> bool:
    """Check if the message was sent by the bot itself."""
    sender = event.event.sender
    if sender and sender.sender_id:
        return sender.sender_id.open_id == settings.bot_open_id
    return False


def do_message_receive(event: P2ImMessageReceiveV1) -> None:
    """Handle im.message.receive_v1 event (sync callback from SDK)."""
    global _loop

    if _is_bot_message(event):
        return

    message = event.event.message
    sender = event.event.sender

    chat_id = message.chat_id
    message_id = message.message_id
    chat_type = message.chat_type  # "p2p" or "group"
    user_open_id = sender.sender_id.open_id if sender and sender.sender_id else ""

    text, image_key = _extract_content(
        {
            "message_type": message.message_type,
            "content": message.content,
        }
    )

    if text is None and image_key is None:
        logger.debug("Ignoring unsupported message: %s", message.message_type)
        return

    logger.info(
        "Message from %s in %s (%s): text=%s image=%s",
        user_open_id,
        chat_id,
        chat_type,
        (text[:100] if text else None),
        image_key,
    )

    if _loop is None:
        logger.error("Event loop not set")
        return

    # Dedup: schedule the async dedup check + dispatch
    asyncio.run_coroutine_threadsafe(
        _dispatch_message(message_id, user_open_id, text, chat_id, chat_type, image_key),
        _loop,
    )


async def _dispatch_message(
    message_id: str,
    user_open_id: str,
    text: str | None,
    chat_id: str,
    chat_type: str,
    image_key: str | None = None,
) -> None:
    """Check dedup then dispatch to the appropriate handler."""
    # Dedup check
    if await store.is_message_seen(message_id):
        logger.info("Duplicate message %s, skipping", message_id)
        return
    await store.mark_message_seen(message_id)

    if chat_type == "p2p":
        await dm.handle_dm_message(message_id, user_open_id, text, chat_id, image_key)
    elif chat_type == "group":
        await group.handle_group_message(
            message_id, user_open_id, text, chat_id, image_key
        )


def do_card_action(event: P2CardActionTrigger) -> P2CardActionTriggerResponse | None:
    """Handle card interaction callback."""
    global _loop

    action = event.event.action
    user_open_id = ""
    if event.event.operator and event.event.operator.open_id:
        user_open_id = event.event.operator.open_id

    if _loop is None:
        return None

    # Run the async handler and wait for result
    future = asyncio.run_coroutine_threadsafe(
        dm.handle_card_action(
            {
                "value": action.value,
                "option": action.option,
                "form_value": getattr(action, "form_value", None),
            },
            user_open_id,
        ),
        _loop,
    )

    try:
        result = future.result(timeout=10)
        if result:
            return P2CardActionTriggerResponse(result)
    except Exception:
        logger.exception("Card action handler failed")

    return None


def build_event_handler() -> lark.EventDispatcherHandler:
    """Build the Feishu event dispatcher with handlers registered."""
    return (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(do_message_receive)
        .register_p2_card_action_trigger(do_card_action)
        .build()
    )


async def _run_ws_client(ws_client: lark.ws.Client) -> None:
    """Run the WebSocket client in a thread (it blocks)."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, ws_client.start)


async def _periodic_cleanup_seen() -> None:
    """Periodically clean up old seen_messages entries."""
    while True:
        await asyncio.sleep(settings.cleanup_interval)
        try:
            await store.cleanup_seen_messages()
        except Exception:
            logger.exception("Cleanup seen_messages failed")


async def _resume_interrupted_sessions() -> None:
    """Resume sessions that were interrupted by the last shutdown.

    Reads active_cards saved before shutdown, updates them, and enqueues
    resume messages through the normal queue path.
    """
    saved = await store.load_and_clear_active_cards()
    if not saved:
        return
    logger.info("Resuming %d interrupted session(s)", len(saved))
    for chat_id, card_msg_id in saved.items():
        binding = await store.get_binding(chat_id)
        if not binding or not binding.session_id:
            logger.info("Skipping resume for %s: no binding or session", chat_id)
            continue
        try:
            card = formatter.build_thinking_card("正在自动恢复...")
            await feishu_client.async_update_card(card_msg_id, card)
        except Exception:
            logger.warning("Failed to update card %s on resume", card_msg_id)
        # Enqueue resume message through the normal queue path
        await store.enqueue_message(chat_id, "", "请继续之前被中断的任务")
        await worker_manager.ensure_worker(chat_id)


async def main() -> None:
    global _loop
    _loop = asyncio.get_event_loop()

    # Initialize DB
    await store.get_db()
    logger.info("Database initialized")

    # Restore workers for chats with pending messages
    pending_chats = await store.get_chats_with_pending_messages()
    for chat_id in pending_chats:
        logger.info("Restoring worker for chat %s", chat_id)
        await worker_manager.ensure_worker(chat_id)

    # Resume sessions interrupted by last shutdown
    await _resume_interrupted_sessions()

    # Start periodic cleanup task
    asyncio.create_task(_periodic_cleanup_seen())

    # Build event handler and WebSocket client
    event_handler = build_event_handler()
    ws_client = lark.ws.Client(
        app_id=settings.feishu_app_id,
        app_secret=settings.feishu_app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.DEBUG if settings.log_level == "DEBUG" else lark.LogLevel.INFO,
    )

    logger.info("Starting Coding Partner bot (Gateway mode)...")

    # Handle shutdown gracefully
    stop_event = asyncio.Event()

    def _shutdown(sig, frame):
        logger.info("Received signal %s, shutting down...", sig)
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Start WebSocket client in background thread
    asyncio.create_task(_run_ws_client(ws_client))

    # Wait for shutdown signal
    await stop_event.wait()

    # Cleanup: shutdown all worker subprocesses
    await worker_manager.shutdown_all_workers()

    await store.close_db()
    logger.info("Shutdown complete")


def _check_dependencies() -> None:
    """Verify required external tools are available before starting."""
    missing = []
    required_tools = [
        (settings.configured_agent_cli, f"{settings.agent_display_name} CLI"),
        ("git", "Git version control"),
    ]
    if settings.normalized_agent_provider == "claude":
        required_tools.append(("script", "PTY allocation for streaming"))

    for cmd, purpose in required_tools:
        if not shutil.which(cmd):
            missing.append(f"  - {cmd} ({purpose})")
    if missing:
        raise SystemExit(
            "Missing required dependencies:\n"
            + "\n".join(missing)
            + "\nPlease install them and try again."
        )


def run() -> None:
    """Entry point for the application."""
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _check_dependencies()
    asyncio.run(main())


if __name__ == "__main__":
    run()
