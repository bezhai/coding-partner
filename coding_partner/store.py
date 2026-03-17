"""SQLite storage for user context and chat bindings."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import aiosqlite

from coding_partner.config import settings

logger = logging.getLogger(__name__)

_db: aiosqlite.Connection | None = None
_lock = asyncio.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_context (
    user_id TEXT PRIMARY KEY,
    repo_path TEXT NOT NULL,
    agent_provider TEXT NOT NULL DEFAULT 'claude',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_binding (
    chat_id TEXT PRIMARY KEY,
    worktree_path TEXT NOT NULL,
    repo_path TEXT NOT NULL,
    branch_name TEXT NOT NULL,
    user_id TEXT NOT NULL,
    agent_provider TEXT NOT NULL DEFAULT 'claude',
    session_id TEXT,
    permission_mode TEXT NOT NULL DEFAULT 'auto',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    text TEXT NOT NULL,
    image_paths TEXT NOT NULL DEFAULT '',
    disallowed_tools TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_plans (
    chat_id TEXT PRIMARY KEY,
    session_id TEXT,
    plan_text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_messages (
    message_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS active_cards (
    chat_id TEXT PRIMARY KEY,
    card_msg_id TEXT NOT NULL
);
"""


async def _migrate(db: aiosqlite.Connection) -> None:
    """Add columns that may be missing in older databases."""
    async with db.execute("PRAGMA table_info(user_context)") as cursor:
        user_columns = {row[1] for row in await cursor.fetchall()}
    if "agent_provider" not in user_columns:
        await db.execute(
            "ALTER TABLE user_context ADD COLUMN agent_provider TEXT NOT NULL DEFAULT 'claude'"
        )
        await db.commit()
        logger.info("Migrated user_context: added agent_provider column")

    async with db.execute("PRAGMA table_info(chat_binding)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    if "permission_mode" not in columns:
        await db.execute(
            "ALTER TABLE chat_binding ADD COLUMN permission_mode TEXT NOT NULL DEFAULT 'auto'"
        )
        await db.commit()
        logger.info("Migrated chat_binding: added permission_mode column")
    if "agent_provider" not in columns:
        await db.execute(
            "ALTER TABLE chat_binding ADD COLUMN agent_provider TEXT NOT NULL DEFAULT 'claude'"
        )
        await db.commit()
        logger.info("Migrated chat_binding: added agent_provider column")

    async with db.execute("PRAGMA table_info(message_queue)") as cursor:
        mq_columns = {row[1] for row in await cursor.fetchall()}
    if "image_paths" not in mq_columns:
        await db.execute(
            "ALTER TABLE message_queue ADD COLUMN image_paths TEXT NOT NULL DEFAULT ''"
        )
        await db.commit()
        logger.info("Migrated message_queue: added image_paths column")
    if "disallowed_tools" not in mq_columns:
        await db.execute(
            "ALTER TABLE message_queue ADD COLUMN disallowed_tools TEXT NOT NULL DEFAULT ''"
        )
        await db.commit()
        logger.info("Migrated message_queue: added disallowed_tools column")


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        async with _lock:
            if _db is None:
                db_path = settings.db_file
                _db = await aiosqlite.connect(str(db_path))
                _db.row_factory = aiosqlite.Row
                await _db.execute("PRAGMA journal_mode=WAL")
                await _db.executescript(SCHEMA)
                await _migrate(_db)
                await _db.commit()
    return _db


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


# --- user_context ---


async def set_user_repo(user_id: str, repo_path: str) -> None:
    db = await get_db()
    now = datetime.now(UTC).isoformat()
    provider = await get_user_agent_provider(user_id)
    await db.execute(
        "INSERT OR REPLACE INTO user_context (user_id, repo_path, agent_provider, updated_at)"
        " VALUES (?, ?, ?, ?)",
        (user_id, repo_path, provider, now),
    )
    await db.commit()


async def get_user_repo(user_id: str) -> str | None:
    db = await get_db()
    async with db.execute(
        "SELECT repo_path FROM user_context WHERE user_id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
        return row["repo_path"] if row else None


async def set_user_agent_provider(user_id: str, agent_provider: str) -> None:
    db = await get_db()
    now = datetime.now(UTC).isoformat()
    repo_path = await get_user_repo(user_id)
    repo_value = repo_path or ""
    await db.execute(
        "INSERT OR REPLACE INTO user_context (user_id, repo_path, agent_provider, updated_at)"
        " VALUES (?, ?, ?, ?)",
        (user_id, repo_value, agent_provider, now),
    )
    await db.commit()


async def get_user_agent_provider(user_id: str) -> str:
    db = await get_db()
    async with db.execute(
        "SELECT agent_provider FROM user_context WHERE user_id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
        if not row:
            return settings.normalized_agent_provider
        provider = (row["agent_provider"] or "").strip().lower()
        return provider if provider in {"claude", "codex"} else settings.normalized_agent_provider


# --- chat_binding ---


@dataclass
class ChatBinding:
    chat_id: str
    worktree_path: str
    repo_path: str
    branch_name: str
    user_id: str
    agent_provider: str
    session_id: str | None
    permission_mode: str  # "auto" or "confirm"
    created_at: str


async def create_binding(
    chat_id: str,
    worktree_path: str,
    repo_path: str,
    branch_name: str,
    user_id: str,
    agent_provider: str | None = None,
) -> None:
    db = await get_db()
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO chat_binding
           (chat_id, worktree_path, repo_path, branch_name, user_id, agent_provider, session_id,
            permission_mode, created_at)
           VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
        (
            chat_id,
            worktree_path,
            repo_path,
            branch_name,
            user_id,
            agent_provider or settings.normalized_agent_provider,
            settings.permission_mode,
            now,
        ),
    )
    await db.commit()


async def get_binding(chat_id: str) -> ChatBinding | None:
    db = await get_db()
    async with db.execute("SELECT * FROM chat_binding WHERE chat_id = ?", (chat_id,)) as cursor:
        row = await cursor.fetchone()
        if not row:
            return None
        return ChatBinding(**dict(row))


async def update_session_id(chat_id: str, session_id: str | None) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE chat_binding SET session_id = ? WHERE chat_id = ?",
        (session_id, chat_id),
    )
    await db.commit()


async def update_permission_mode(chat_id: str, mode: str) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE chat_binding SET permission_mode = ? WHERE chat_id = ?",
        (mode, chat_id),
    )
    await db.commit()


async def delete_binding(chat_id: str) -> None:
    db = await get_db()
    await db.execute("DELETE FROM chat_binding WHERE chat_id = ?", (chat_id,))
    await db.commit()


# --- message_queue ---


@dataclass
class QueuedMessage:
    id: int
    chat_id: str
    message_id: str
    text: str
    image_paths: str  # comma-separated local file paths, empty string if none
    disallowed_tools: str  # empty=use binding default, '[]'=explicitly allow all
    created_at: str


async def enqueue_message(
    chat_id: str, message_id: str, text: str, image_paths: str = "",
    disallowed_tools: str = "",
) -> None:
    db = await get_db()
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO message_queue (chat_id, message_id, text, image_paths, disallowed_tools, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, message_id, text, image_paths, disallowed_tools, now),
    )
    await db.commit()


async def dequeue_message(chat_id: str) -> QueuedMessage | None:
    db = await get_db()
    async with db.execute(
        "SELECT * FROM message_queue WHERE chat_id = ? ORDER BY id LIMIT 1",
        (chat_id,),
    ) as cursor:
        row = await cursor.fetchone()
        if not row:
            return None
        return QueuedMessage(**dict(row))


async def delete_queued_message(msg_id: int) -> None:
    db = await get_db()
    await db.execute("DELETE FROM message_queue WHERE id = ?", (msg_id,))
    await db.commit()


async def clear_queue(chat_id: str) -> int:
    """Clear all queued messages for a chat. Return count deleted."""
    db = await get_db()
    async with db.execute(
        "SELECT COUNT(*) as cnt FROM message_queue WHERE chat_id = ?", (chat_id,)
    ) as cursor:
        row = await cursor.fetchone()
        count = row["cnt"] if row else 0
    await db.execute("DELETE FROM message_queue WHERE chat_id = ?", (chat_id,))
    await db.commit()
    return count


async def get_chats_with_pending_messages() -> list[str]:
    """Return distinct chat_ids that have pending messages."""
    db = await get_db()
    async with db.execute("SELECT DISTINCT chat_id FROM message_queue") as cursor:
        rows = await cursor.fetchall()
        return [row["chat_id"] for row in rows]


# --- seen_messages (dedup) ---


async def is_message_seen(message_id: str) -> bool:
    db = await get_db()
    async with db.execute(
        "SELECT 1 FROM seen_messages WHERE message_id = ?", (message_id,)
    ) as cursor:
        return await cursor.fetchone() is not None


async def mark_message_seen(message_id: str) -> None:
    db = await get_db()
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT OR IGNORE INTO seen_messages (message_id, created_at) VALUES (?, ?)",
        (message_id, now),
    )
    await db.commit()


async def save_active_cards(cards: dict[str, str]) -> None:
    """Persist active streaming cards so they can be resumed after restart."""
    db = await get_db()
    await db.execute("DELETE FROM active_cards")
    for chat_id, card_msg_id in cards.items():
        await db.execute(
            "INSERT INTO active_cards (chat_id, card_msg_id) VALUES (?, ?)",
            (chat_id, card_msg_id),
        )
    await db.commit()


async def load_and_clear_active_cards() -> dict[str, str]:
    """Load active cards saved before last shutdown and clear the table."""
    db = await get_db()
    async with db.execute("SELECT chat_id, card_msg_id FROM active_cards") as cursor:
        rows = await cursor.fetchall()
    await db.execute("DELETE FROM active_cards")
    await db.commit()
    return {row["chat_id"]: row["card_msg_id"] for row in rows}


# --- pending_plans ---


async def save_pending_plan(chat_id: str, session_id: str | None, plan_text: str) -> None:
    db = await get_db()
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT OR REPLACE INTO pending_plans (chat_id, session_id, plan_text, created_at)"
        " VALUES (?, ?, ?, ?)",
        (chat_id, session_id, plan_text, now),
    )
    await db.commit()


async def get_pending_plan(chat_id: str) -> dict | None:
    """Return {session_id, plan_text} or None."""
    db = await get_db()
    async with db.execute(
        "SELECT session_id, plan_text FROM pending_plans WHERE chat_id = ?", (chat_id,)
    ) as cursor:
        row = await cursor.fetchone()
        if not row:
            return None
        return {"session_id": row["session_id"], "plan_text": row["plan_text"]}


async def delete_pending_plan(chat_id: str) -> None:
    db = await get_db()
    await db.execute("DELETE FROM pending_plans WHERE chat_id = ?", (chat_id,))
    await db.commit()


async def cleanup_seen_messages(max_age_seconds: int | None = None) -> None:
    """Remove seen_messages older than max_age_seconds."""
    if max_age_seconds is None:
        max_age_seconds = settings.seen_message_max_age
    db = await get_db()
    await db.execute(
        "DELETE FROM seen_messages WHERE datetime(created_at) < datetime('now', ?)",
        (f"-{max_age_seconds} seconds",),
    )
    await db.commit()
