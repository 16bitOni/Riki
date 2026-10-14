"""LangGraph checkpointer: persists full graph state (messages, tool calls, images) across turns.

Uses the same Postgres database as the rest of the app but through psycopg (not asyncpg),
because langgraph-checkpoint-postgres requires it. The checkpointer creates its own tables
on setup() — no Alembic migration needed for those.

We use the sync PostgresSaver with async wrappers because psycopg's native async mode is
incompatible with Windows' ProactorEventLoop. The sync connection runs in threads via
asyncio.to_thread, so it doesn't block the event loop.
"""

import asyncio
import logging

from psycopg import Connection
from langgraph.checkpoint.postgres import PostgresSaver

from app.config import settings

log = logging.getLogger(__name__)

_saver: "AsyncWrappedSaver | None" = None
_conn: Connection | None = None


class AsyncWrappedSaver(PostgresSaver):
    """Thin wrapper: provides async methods by running sync ones in a thread."""

    async def aget_tuple(self, config):
        return await asyncio.to_thread(self.get_tuple, config)

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        return await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def alist(self, config, *, filter=None, before=None, limit=None):
        results = await asyncio.to_thread(
            lambda: list(self.list(config, filter=filter, before=before, limit=limit))
        )
        for item in results:
            yield item


def init_checkpointer() -> AsyncWrappedSaver:
    """Call once at startup. Creates checkpoint tables if they don't exist."""
    global _saver, _conn
    _conn = Connection.connect(
        settings.checkpoint_database_url, autocommit=True
    )
    _saver = AsyncWrappedSaver(conn=_conn)
    _saver.setup()
    log.info("LangGraph checkpointer ready (Postgres)")
    return _saver


def get_checkpointer() -> AsyncWrappedSaver | None:
    return _saver


def close_checkpointer() -> None:
    global _saver, _conn
    if _conn is not None:
        _conn.close()
        _conn = None
    _saver = None
    log.info("Checkpointer closed")
