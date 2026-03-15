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
           (chat_id, worktree_path, repo_path, branch_name, user_id, session_id, created_at)
           VALUES (?, ?, ?, ?, ?, NULL, ?)""",
        (chat_id, worktree_path, repo_path, branch_name, user_id, now),
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


async def delete_binding(chat_id: str) -> None:
    db = await get_db()
    await db.execute("DELETE FROM chat_binding WHERE chat_id = ?", (chat_id,))
    await db.commit()
