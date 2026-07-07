"""
Global background-task registry.

Every fire-and-forget coroutine in the app (WS event publishes, push
notifications, assignment timeout watchers, etc.) MUST be scheduled via
`track_task()` instead of a bare `asyncio.ensure_future()` /
`asyncio.create_task()`. This keeps a live reference to the task so that
`cancel_all_background_tasks()` (called from main.py's lifespan shutdown)
can cancel every outstanding task cleanly before the event loop closes.

Without this, any in-flight task (most commonly an assignment
`_timeout_watcher` sleeping for several minutes, or a Redis publish) is
left dangling on Ctrl+C. On Windows in particular, an orphaned task still
holding an asyncpg/Redis socket is a common cause of uvicorn hanging on
shutdown instead of actually exiting.
"""

import asyncio
import logging
from typing import Coroutine, Set

logger = logging.getLogger(__name__)

_background_tasks: Set[asyncio.Task] = set()


def track_task(coro: Coroutine) -> asyncio.Task:
    """
    Schedule `coro` to run in the background (same behavior as
    asyncio.ensure_future), but keep a reference so it can be cancelled
    on shutdown. Use this everywhere instead of asyncio.ensure_future()
    or asyncio.create_task() for fire-and-forget work.
    """
    task = asyncio.ensure_future(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def cancel_all_background_tasks(timeout: float = 5.0) -> None:
    """
    Cancel every tracked, still-pending background task and wait (up to
    `timeout` seconds total) for them to actually unwind. Call this from
    the FastAPI lifespan shutdown handler, before disposing the DB engine.
    """
    tasks = [t for t in _background_tasks if not t.done()]
    if not tasks:
        return

    logger.info(f"[SHUTDOWN] Cancelling {len(tasks)} outstanding background task(s)")
    for t in tasks:
        t.cancel()

    done, pending = await asyncio.wait(tasks, timeout=timeout)

    if pending:
        logger.warning(
            f"[SHUTDOWN] {len(pending)} background task(s) did not exit within "
            f"{timeout}s and were abandoned: {[t.get_name() for t in pending]}"
        )
