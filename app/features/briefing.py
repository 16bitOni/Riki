"""Morning briefing: a structured daily digest sent via Telegram.

Gathers today's calendar, open tasks, pending reminders, and reflection
highlights, then lets the LLM compose a concise morning message.
"""

import asyncio
import logging
from datetime import datetime, timedelta, time as dt_time, timezone
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage, SystemMessage

from app.config import settings
from app.core.db import session_scope
from app.core.llm import chat_model, text_of
from app.core.models import ProactiveNudge
from app.features.planner import list_tasks
from app.cognition import get_latest_reflection
from app.features.reminders import list_pending
from app.core.timeutil import now_in
from app.tools.google_common import google_service

log = logging.getLogger(__name__)

BRIEFING_PROMPT = """You are Riki. Write a short morning briefing for {name}.
It's {current_time}. Be direct and casual — like a sharp friend giving a quick rundown, not a corporate digest.

Format: start with a one-line greeting that fits the day/mood, then the substance.
If calendar is empty and there's nothing pressing, keep it very short — don't pad.
Use bullet points only when there are 3+ items in a category.
Skip any category that's empty — don't say "no tasks" or "nothing on calendar" unless everything is empty.
If there are habits to do today, mention them naturally — "don't forget your run" not "habit checklist:".

Data below:

Calendar today:
{calendar}

Open tasks:
{tasks}

Pending reminders:
{reminders}

Habits to do today:
{habits}

Your read on {name}:
{reflection}"""


def _fetch_today_calendar(tz: str) -> str:
    """Get today's calendar events. Runs in a thread (Google client is sync)."""
    service = google_service("calendar", "v3")
    if service is None or isinstance(service, str):
        return "(calendar not connected)"
    zone = ZoneInfo(tz)
    now = datetime.now(zone)
    start = datetime.combine(now.date(), dt_time.min, tzinfo=zone).isoformat()
    end = datetime.combine(now.date() + timedelta(days=1), dt_time.min, tzinfo=zone).isoformat()
    try:
        items = service.events().list(
            calendarId="primary", timeMin=start, timeMax=end,
            singleEvents=True, orderBy="startTime",
        ).execute().get("items", [])
    except Exception as e:
        log.warning("Calendar fetch failed: %s", e)
        return "(couldn't reach calendar)"
    if not items:
        return "(nothing scheduled)"
    return "\n".join(
        f"- {e.get('summary', '(no title)')}: {e['start'].get('dateTime', e['start'].get('date'))}"
        for e in items
    )


async def _gather_data(user) -> dict:
    from app.features.habits import list_habits as get_habits

    calendar, tasks, reminders, reflection, habits = await asyncio.gather(
        asyncio.to_thread(_fetch_today_calendar, user.timezone),
        list_tasks(user.id, "open"),
        list_pending(user.id),
        get_latest_reflection(user.id),
        get_habits(user.id),
    )
    task_text = "\n".join(
        f"- {t.title}" + (f" (due {t.due_at.strftime('%b %d')})" if t.due_at else "")
        for t in tasks
    ) if tasks else "(none)"
    reminder_text = "\n".join(
        f"- {r.message} (at {r.due_at.strftime('%I:%M %p')})" for r in reminders
    ) if reminders else "(none)"
    reflection_text = reflection.content if reflection and reflection.content else "(no reflection yet)"
    habits_text = ", ".join(h.name for h in habits) if habits else "(none tracked yet)"
    return {
        "calendar": calendar,
        "tasks": task_text,
        "reminders": reminder_text,
        "habits": habits_text,
        "reflection": reflection_text,
    }


async def _already_sent_today(user_id) -> bool:
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    async with session_scope() as session:
        from sqlalchemy import select
        result = await session.scalar(
            select(ProactiveNudge.id).where(
                ProactiveNudge.user_id == user_id,
                ProactiveNudge.kind == "briefing",
                ProactiveNudge.sent_at >= today_start,
            ).limit(1)
        )
    return result is not None


async def send_morning_briefing(user, notify_fn) -> bool:
    """Compose and send the morning briefing. Returns True if sent."""
    if await _already_sent_today(user.id):
        log.debug("Briefing already sent today, skipping")
        return False

    data = await _gather_data(user)
    current_time = now_in(user.timezone).strftime("%A %Y-%m-%d %I:%M %p")

    llm = chat_model(fast=True, purpose="briefing")
    reply = await llm.ainvoke([
        SystemMessage(BRIEFING_PROMPT.format(name=user.name, current_time=current_time, **data)),
        HumanMessage("Write the morning briefing."),
    ])
    message = text_of(reply).strip()
    if not message:
        log.warning("Briefing LLM returned empty, skipping")
        return False

    telegram_message_id = await notify_fn(message)
    log.info("Morning briefing sent, msg_id=%s", telegram_message_id)

    async with session_scope() as session:
        session.add(ProactiveNudge(
            user_id=user.id,
            content=message,
            topic="morning_briefing",
            kind="briefing",
            telegram_message_id=telegram_message_id,
        ))
    return True
