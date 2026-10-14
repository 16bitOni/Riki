"""Reminder tools for the planner: a short Telegram message at a chosen time."""

from datetime import datetime
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.features import reminders
from app.core.schemas import ShortText
from app.core.timeutil import localize, now_in


def _when(due: datetime) -> str:
    return due.strftime("%a %d %b %Y, %H:%M")


@tool
async def set_reminder(text: ShortText, when: datetime, *, config: RunnableConfig) -> str:
    """Remind the user on Telegram at a given time. `text` is what the message should say (e.g. "Buy milk");
    `when` is ISO 8601 with offset, e.g. 2026-10-06T21:00:00+05:30."""
    user = config["configurable"]["user"]
    due = localize(when, user.timezone)
    if due <= now_in(user.timezone):
        return "That time is already past. Ask the user for a time in the future."
    reminder = await reminders.create_reminder(user.id, text, due)
    warning = "" if reminders.instance else " (Telegram is not running, so it will only be sent once the bot is on.)"
    return f"Reminder set for {_when(due)} (id: {reminder.id}).{warning}"


@tool
async def list_reminders(*, config: RunnableConfig) -> str:
    """List the user's upcoming reminders, soonest first."""
    user = config["configurable"]["user"]
    pending = await reminders.list_pending(user.id)
    zone = now_in(user.timezone).tzinfo
    return "\n".join(f"- {_when(r.due_at.astimezone(zone))}: {r.message}  (id: {r.id})" for r in pending) or "No reminders."


@tool
async def cancel_reminder(reminder_id: UUID, *, config: RunnableConfig) -> str:
    """Cancel an upcoming reminder, using the id shown by list_reminders."""
    user = config["configurable"]["user"]
    done = await reminders.cancel_reminder(user.id, reminder_id)
    return "Reminder cancelled." if done else "No upcoming reminder with that id."
