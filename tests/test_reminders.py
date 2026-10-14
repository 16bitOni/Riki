"""Reminders against the real database; only the Telegram send is replaced."""

import asyncio
from datetime import timedelta

from app import reminders
from app.models import utcnow


async def test_due_reminder_is_sent_once_and_future_one_is_not(user):
    sent = []

    async def send(text):
        sent.append(text)

    scheduler = reminders.ReminderScheduler(send)
    task = asyncio.create_task(scheduler.run())
    try:
        await reminders.create_reminder(user.id, "far away", utcnow() + timedelta(days=1))
        await reminders.create_reminder(user.id, "buy milk", utcnow() + timedelta(seconds=1))
        for _ in range(50):  # the scheduler sleeps until it is due; no polling to wait on
            if sent:
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.3)
    finally:
        task.cancel()
        reminders.instance = None

    assert sent == ["⏰ Reminder: buy milk"]
    assert [r.message for r in await reminders.list_pending(user.id)] == ["far away"]


async def test_overdue_reminder_is_sent_late_and_cancel_works(user):
    sent = []

    async def send(text):
        sent.append(text)

    overdue = await reminders.create_reminder(user.id, "missed while off", utcnow() - timedelta(hours=2))
    cancelled = await reminders.create_reminder(user.id, "never mind", utcnow() + timedelta(hours=1))
    assert await reminders.cancel_reminder(user.id, cancelled.id)
    assert not await reminders.cancel_reminder(user.id, cancelled.id)  # already cancelled

    scheduler = reminders.ReminderScheduler(send)
    assert await scheduler._deliver_due()
    reminders.instance = None
    assert len(sent) == 1 and "missed while off" in sent[0] and "was due" in sent[0]
    assert await reminders.list_pending(user.id) == []


async def test_failed_send_keeps_reminder_pending(user):
    async def broken(text):
        raise RuntimeError("telegram down")

    await reminders.create_reminder(user.id, "retry me", utcnow() - timedelta(minutes=1))
    scheduler = reminders.ReminderScheduler(broken)
    assert not await scheduler._deliver_due()
    reminders.instance = None
    assert len(await reminders.list_pending(user.id)) == 1
