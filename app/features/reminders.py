"""Reminders: stored in the database, delivered by one background task.

There is no polling. The task asks the database for the next due time and sleeps until then;
with nothing pending it sleeps until a new reminder wakes it. Reminders survive restarts, and
ones that came due while the server was off are sent right away with a "late" note.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update

from app.core.db import session_scope
from app.core.models import Reminder, utcnow

log = logging.getLogger(__name__)

MAX_SLEEP = 3600  # wake at least hourly, so a changed system clock can never strand a reminder
RETRY_SECONDS = 60  # when sending fails (Telegram down), try again in a minute
LATE_AFTER = timedelta(minutes=5)

instance: "ReminderScheduler | None" = None  # the running scheduler, so the tools can wake it


async def create_reminder(user_id: UUID, text: str, due_at: datetime) -> Reminder:
    async with session_scope() as session:
        reminder = Reminder(user_id=user_id, message=text, due_at=due_at)
        session.add(reminder)
        await session.flush()
    if instance:
        instance.poke()
    return reminder


async def list_pending(user_id: UUID) -> list[Reminder]:
    async with session_scope() as session:
        rows = await session.scalars(
            select(Reminder).where(Reminder.user_id == user_id, Reminder.status == "pending").order_by(Reminder.due_at)
        )
        return list(rows)


async def cancel_reminder(user_id: UUID, reminder_id: UUID) -> bool:
    async with session_scope() as session:
        result = await session.execute(
            update(Reminder)
            .where(Reminder.id == reminder_id, Reminder.user_id == user_id, Reminder.status == "pending")
            .values(status="cancelled")
        )
    if instance:
        instance.poke()  # the cancelled one may have been the next due
    return result.rowcount > 0


class ReminderScheduler:
    def __init__(self, send):
        """`send` is an async function taking the message text."""
        global instance
        self.send = send
        self._wake = asyncio.Event()
        instance = self

    def poke(self) -> None:
        self._wake.set()

    async def _next_due(self) -> datetime | None:
        async with session_scope() as session:
            return await session.scalar(
                select(Reminder.due_at).where(Reminder.status == "pending").order_by(Reminder.due_at).limit(1)
            )

    async def _deliver_due(self) -> bool:
        """Send everything that is due. Returns False if a send failed (the reminder stays pending)."""
        async with session_scope() as session:
            due = list(
                await session.scalars(
                    select(Reminder)
                    .where(Reminder.status == "pending", Reminder.due_at <= utcnow())
                    .order_by(Reminder.due_at)
                )
            )
        for reminder in due:
            late = utcnow() - reminder.due_at > LATE_AFTER
            note = f" (was due {reminder.due_at.astimezone().strftime('%a %d %b, %H:%M')})" if late else ""
            try:
                await self.send(f"⏰ Reminder: {reminder.message}{note}")
            except Exception as error:
                log.warning("Reminder send failed (%s); will retry", type(error).__name__)
                return False
            async with session_scope() as session:
                await session.execute(
                    update(Reminder).where(Reminder.id == reminder.id).values(status="sent", sent_at=utcnow())
                )
        return True

    async def run(self) -> None:
        log.info("Reminder scheduler started")
        while True:
            self._wake.clear()  # cleared before the lookup, so a reminder added meanwhile is never missed
            try:
                next_due = await self._next_due()
                wait = None if next_due is None else min((next_due - utcnow()).total_seconds(), MAX_SLEEP)
                if wait is None or wait > 0:
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=wait)
                    except asyncio.TimeoutError:
                        pass
                if not await self._deliver_due():
                    await asyncio.sleep(RETRY_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Reminder scheduler error; retrying")
                await asyncio.sleep(RETRY_SECONDS)
