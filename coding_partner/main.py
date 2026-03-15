"""Entry point: Feishu WebSocket bot with event routing."""

import asyncio
import json
import logging
import signal

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model import P2CardActionTrigger, P2CardActionTriggerResponse

from coding_partner import store
from coding_partner.config import settings
from coding_partner.handlers import dm, group

logger = logging.getLogger(__name__)

# asyncio event loop reference for scheduling coroutines from sync callbacks
_loop: asyncio.AbstractEventLoop | None = None


def _extract_text(message: dict) -> str | None:
    """Extract plain text from a message content JSON."""
    msg_type = message.get("message_type", "")
    content_str = message.get("content", "{}")

    if msg_type != "text":
        return None

    try:
        content = json.loads(content_str)
        return content.get("text", "").strip()
    except (json.JSONDecodeError, AttributeError):
        return None


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

    text = _extract_text(
        {
            "message_type": message.message_type,
            "content": message.content,
        }
    )

    if text is None:
        logger.debug("Ignoring non-text message: %s", message.message_type)
        return

    logger.info(
        "Message from %s in %s (%s): %s",
        user_open_id,
        chat_id,
        chat_type,
        text[:100],
    )

    if _loop is None:
        logger.error("Event loop not set")
        return

    if chat_type == "p2p":
        asyncio.run_coroutine_threadsafe(
            dm.handle_dm_message(message_id, user_open_id, text, chat_id),
            _loop,
        )
    elif chat_type == "group":
        asyncio.run_coroutine_threadsafe(
            group.handle_group_message(message_id, user_open_id, text, chat_id),
            _loop,
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
            {"value": action.value, "option": action.option},
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


async def main() -> None:
    global _loop
    _loop = asyncio.get_event_loop()

    # Initialize DB
    await store.get_db()
    logger.info("Database initialized")

    # Build event handler and WebSocket client
    event_handler = build_event_handler()
    ws_client = lark.ws.Client(
        app_id=settings.feishu_app_id,
        app_secret=settings.feishu_app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.DEBUG if settings.log_level == "DEBUG" else lark.LogLevel.INFO,
    )

    logger.info("Starting Coding Partner bot (WebSocket mode)...")

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

    # Cleanup
    await store.close_db()
    logger.info("Shutdown complete")


def run() -> None:
    """Entry point for the application."""
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    asyncio.run(main())


if __name__ == "__main__":
    run()
