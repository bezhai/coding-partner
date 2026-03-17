"""Worker lifecycle management.

Spawns per-chat Worker subprocesses and monitors their lifetime.
Independent module to avoid circular imports between main.py and group.py.
"""

import asyncio
import logging
import signal
import sys

from coding_partner import store

logger = logging.getLogger(__name__)

# Active worker processes: chat_id -> Process
_workers: dict[str, asyncio.subprocess.Process] = {}

# Monitor tasks: chat_id -> Task (watching proc.wait())
_monitors: dict[str, asyncio.Task] = {}


async def ensure_worker(chat_id: str) -> None:
    """If no active Worker exists for this chat, spawn one."""
    proc = _workers.get(chat_id)
    if proc is not None and proc.returncode is None:
        return  # still running
    await spawn_worker(chat_id)


async def spawn_worker(chat_id: str) -> None:
    """Spawn a new Worker subprocess for the given chat."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "coding_partner.worker", chat_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _workers[chat_id] = proc
    logger.info("Spawned worker pid=%d for chat %s", proc.pid, chat_id)

    # Start background monitor
    task = _monitors.get(chat_id)
    if task and not task.done():
        task.cancel()
    _monitors[chat_id] = asyncio.create_task(_monitor_worker(chat_id, proc))


async def kill_worker(chat_id: str, sig: int = signal.SIGTERM) -> None:
    """Send a signal to the Worker process for this chat."""
    proc = _workers.get(chat_id)
    if proc is None or proc.returncode is not None:
        _workers.pop(chat_id, None)
        return
    try:
        proc.send_signal(sig)
        logger.info("Sent signal %s to worker pid=%d for chat %s", sig, proc.pid, chat_id)
    except ProcessLookupError:
        _workers.pop(chat_id, None)


async def wait_worker(chat_id: str, timeout: float = 5.0) -> None:
    """Wait for a worker to exit (after sending it a signal)."""
    proc = _workers.get(chat_id)
    if proc is None or proc.returncode is not None:
        _workers.pop(chat_id, None)
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("Worker pid=%d did not exit in %.1fs, killing", proc.pid, timeout)
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    _workers.pop(chat_id, None)


async def shutdown_all_workers() -> None:
    """Gracefully stop all workers: SIGTERM → wait → SIGKILL."""
    if not _workers:
        return
    logger.info("Shutting down %d worker(s)...", len(_workers))

    # Send SIGTERM to all
    for chat_id in list(_workers):
        await kill_worker(chat_id, signal.SIGTERM)

    # Wait up to 5s for all to exit
    procs = list(_workers.values())
    if procs:
        done, pending = await asyncio.wait(
            [asyncio.create_task(p.wait()) for p in procs],
            timeout=5.0,
        )
        # SIGKILL any stragglers
        for chat_id, proc in list(_workers.items()):
            if proc.returncode is None:
                try:
                    proc.kill()
                    logger.warning("Killed worker pid=%d for chat %s", proc.pid, chat_id)
                except ProcessLookupError:
                    pass

    _workers.clear()

    # Cancel monitor tasks
    for task in _monitors.values():
        if not task.done():
            task.cancel()
    _monitors.clear()


async def _monitor_worker(chat_id: str, proc: asyncio.subprocess.Process) -> None:
    """Wait for a worker to exit, then check if more work is queued."""
    try:
        returncode = await proc.wait()
        # Log stderr if non-zero exit
        if returncode != 0 and proc.stderr:
            stderr = await proc.stderr.read()
            if stderr:
                logger.warning(
                    "Worker pid=%d for chat %s exited with code %d: %s",
                    proc.pid, chat_id, returncode, stderr.decode(errors="replace")[-500:],
                )
        else:
            logger.info("Worker pid=%d for chat %s exited with code %d", proc.pid, chat_id, returncode)
    except asyncio.CancelledError:
        return
    finally:
        # Only clean up if this proc is still the tracked one
        if _workers.get(chat_id) is proc:
            _workers.pop(chat_id, None)
        _monitors.pop(chat_id, None)

    # If there are still pending messages, respawn
    try:
        pending = await store.get_chats_with_pending_messages()
        if chat_id in pending:
            logger.info("Worker for %s exited but queue not empty, respawning", chat_id)
            await spawn_worker(chat_id)
    except Exception:
        logger.exception("Error checking pending messages after worker exit for %s", chat_id)
