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
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_binding (
    chat_id TEXT PRIMARY KEY,
    worktree_path TEXT NOT NULL,
    repo_path TEXT NOT NULL,
    branch_name TEXT NOT NULL,
    user_id TEXT NOT NULL,
    session_id TEXT,
    permission_mode TEXT NOT NULL DEFAULT 'auto',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_messages (
    message_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
"""


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        async with _lock:
            if _db is None:
                db_path = settings.db_file
                _db = await aiosqlite.connect(str(db_path))
                _db.row_factory = aiosqlite.Row
                await _db.executescript(SCHEMA)
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
    await db.execute(
        "INSERT OR REPLACE INTO user_context (user_id, repo_path, updated_at) VALUES (?, ?, ?)",
        (user_id, repo_path, now),
    )
    await db.commit()


async def get_user_repo(user_id: str) -> str | None:
    db = await get_db()
    async with db.execute(
        "SELECT repo_path FROM user_context WHERE user_id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
        return row["repo_path"] if row else None


# --- chat_binding ---


@dataclass
class ChatBinding:
    chat_id: str
    worktree_path: str
    repo_path: str
    branch_name: str
    user_id: str
    session_id: str | None
    permission_mode: str  # "auto" or "confirm"
    created_at: str


async def create_binding(
    chat_id: str,
    worktree_path: str,
    repo_path: str,
    branch_name: str,
    user_id: str,
) -> None:
    db = await get_db()
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO chat_binding
           (chat_id, worktree_path, repo_path, branch_name, user_id, session_id,
            permission_mode, created_at)
           VALUES (?, ?, ?, ?, ?, NULL, ?, ?)""",
        (chat_id, worktree_path, repo_path, branch_name, user_id,
         settings.permission_mode, now),
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
    created_at: str


async def enqueue_message(chat_id: str, message_id: str, text: str) -> None:
    db = await get_db()
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO message_queue (chat_id, message_id, text, created_at) VALUES (?, ?, ?, ?)",
        (chat_id, message_id, text, now),
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
