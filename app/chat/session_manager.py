"""Background session lifecycle: timeout detection, crash recovery, graceful shutdown.

Like the ReminderScheduler, this runs as a single background task. It wakes periodically
to check for conversations that have gone quiet and should be closed.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.config import settings
from app.core.db import get_or_create_user, session_scope
from app.core.models import Conversation, Message, SessionGuide
from app.chat.session import close_session

log = logging.getLogger(__name__)

CHECK_INTERVAL = 600  # check every 10 minutes
instance: "SessionManager | None" = None


class SessionManager:
    def __init__(self):
        global instance
        instance = self
        self._stop = asyncio.Event()

    async def _stale_conversations(self) -> list[tuple]:
        """Find conversations that timed out: have messages but no session guide,
        and the last message is older than session_timeout_hours."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.session_timeout_hours)
        async with session_scope() as session:
            # Subquery: conversations with their last message time
            last_msg = (
                select(
                    Message.conversation_id,
                    func.max(Message.created_at).label("last_at"),
                    func.count().label("msg_count"),
                )
                .group_by(Message.conversation_id)
                .subquery()
            )
            # Conversations that timed out and have no guide yet
            rows = (
                await session.execute(
                    select(Conversation.id, Conversation.user_id, last_msg.c.last_at)
                    .join(last_msg, last_msg.c.conversation_id == Conversation.id)
                    .outerjoin(SessionGuide, SessionGuide.conversation_id == Conversation.id)
                    .where(
                        SessionGuide.id.is_(None),
                        last_msg.c.last_at < cutoff,
                        last_msg.c.msg_count >= 4,
                    )
                    .order_by(last_msg.c.last_at)
                    .limit(5)  # process a few at a time
                )
            ).all()
        return rows

    async def _process_stale(self) -> int:
        """Close timed-out sessions. Returns count processed."""
        rows = await self._stale_conversations()
        if not rows:
            return 0
        user = await get_or_create_user()
        closed = 0
        log.info("Found %d timed-out session(s) to close", len(rows))
        for conversation_id, user_id, last_at in rows:
            try:
                await close_session(user, conversation_id)
                closed += 1
            except Exception:
                log.exception("Failed to close timed-out session %s", conversation_id)
        return closed

    async def recover_crashed_sessions(self) -> int:
        """On startup, find conversations that were never closed (no guide, old messages).
        Same logic as timeout, but runs once immediately."""
        count = await self._process_stale()
        if count:
            log.info("Crash recovery: closed %d orphaned session(s)", count)
        return count

    async def shutdown_close_active(self) -> int:
        """On graceful shutdown, close all active sessions (recent messages, no guide)."""
        async with session_scope() as session:
            last_msg = (
                select(
                    Message.conversation_id,
                    func.max(Message.created_at).label("last_at"),
                    func.count().label("msg_count"),
                )
                .group_by(Message.conversation_id)
                .subquery()
            )
            rows = (
                await session.execute(
                    select(Conversation.id, Conversation.user_id)
                    .join(last_msg, last_msg.c.conversation_id == Conversation.id)
                    .outerjoin(SessionGuide, SessionGuide.conversation_id == Conversation.id)
                    .where(SessionGuide.id.is_(None), last_msg.c.msg_count >= 4)
                    .limit(20)
                )
            ).all()
        if not rows:
            return 0
        user = await get_or_create_user()
        closed = 0
        for conversation_id, user_id in rows:
            try:
                await close_session(user, conversation_id)
                closed += 1
            except Exception:
                log.exception("Shutdown: failed to close session %s", conversation_id)
        log.info("Shutdown: closed %d active session(s)", closed)
        return closed

    async def run(self) -> None:
        log.info("Session manager started (timeout=%dh)", settings.session_timeout_hours)
        # Run crash recovery on startup
        await self.recover_crashed_sessions()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=CHECK_INTERVAL)
                break  # stop was set
            except asyncio.TimeoutError:
                pass
            try:
                await self._process_stale()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Session manager check failed")

    def stop(self):
        self._stop.set()
