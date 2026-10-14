"""Proactive nudge engine: Riki reaches out without being asked.

Reads the latest reflection and decides whether to send a genuine, specific
message via Telegram. Respects a minimum interval and stays quiet when the
user has been active recently.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.config import settings
from app.core.db import session_scope
from app.core.llm import chat_model
from app.core.models import Conversation, Message, ProactiveNudge, UserReflection
from app.cognition import get_latest_reflection

log = logging.getLogger(__name__)

NUDGE_PROMPT = """You are Riki. Below is your reflection on {name} and what's been going on recently.
Right now it's {current_time}.

Decide whether to send ONE message to {name}.

Timing matters:
- Morning (8-11am): good for "what's the plan today?" or following up on yesterday
- Afternoon (12-5pm): skip unless something is time-sensitive
- Evening (6-10pm): good for "how did X go?" or winding-down check-ins. If habits are unlogged,
  casually ask about them — "did you get that run in?" not "please log your habits".
- Night (11pm-7am): almost never send. Only for something genuinely urgent.
- Weekends: lighter tone, less work stuff unless they brought it up

A good nudge:
- References something SPECIFIC (a real name, event, commitment, feeling)
- Feels like a friend who remembered, not a bot on a schedule
- 1-2 sentences, casual, direct
- Matches the energy of the time of day

A bad nudge:
- Generic ("how are you doing?", "just checking in!")
- Repeating a recent nudge topic
- Asking about something clearly resolved
- Wrong energy for the time (work pressure at 9pm, casual banter at 9am on a deadline day)

Recent nudges (DO NOT repeat these topics):
{recent_nudges}

Recent conversations:
{recent_conversations}

Unlogged habits today:
{unlogged_habits}

Reflection:
{reflection}

Set should_send=False if: nothing specific, wrong time, already nudged, or they've been active
and probably don't need you reaching out."""


class NudgePlan(BaseModel):
    should_send: bool = Field(
        description="True if there is something genuinely worth reaching out about right now"
    )
    message: str = Field(
        default="",
        description="The message to send. Conversational, specific, 1-2 sentences max. Empty if should_send is False.",
    )
    topic: str = Field(
        default="",
        description="Short label for what this is about, for dedup (e.g. 'auth feature ship', 'Pune trip')",
    )
    kind: Literal["open_loop", "curiosity", "checkin"] = Field(default="open_loop")


async def _get_recent_nudges(user_id: UUID, hours: int = 168) -> list[ProactiveNudge]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with session_scope() as session:
        rows = (
            await session.scalars(
                select(ProactiveNudge)
                .where(ProactiveNudge.user_id == user_id, ProactiveNudge.sent_at >= cutoff)
                .order_by(ProactiveNudge.sent_at.desc())
            )
        ).all()
    return list(rows)


async def _was_active_recently(user_id: UUID, hours: int) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with session_scope() as session:
        result = await session.scalar(
            select(Message.id)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(
                Conversation.user_id == user_id,
                Message.role == "user",
                Message.created_at >= cutoff,
            )
            .limit(1)
        )
    return result is not None


async def _get_recent_conversation_summaries(user_id: UUID, limit: int = 3) -> str:
    """Short summaries of recent conversations for nudge context."""
    async with session_scope() as session:
        convos = (
            await session.scalars(
                select(Conversation)
                .where(Conversation.user_id == user_id)
                .order_by(Conversation.created_at.desc())
                .limit(limit)
            )
        ).all()
    if not convos:
        return "(no recent conversations)"
    lines = []
    for c in convos:
        age = datetime.now(timezone.utc) - c.created_at
        ago = f"{age.days}d ago" if age.days else f"{age.seconds // 3600}h ago"
        lines.append(f"- {ago}: {c.title or '(untitled)'}")
    return "\n".join(lines)


async def plan_nudge(user, reflection_content: str) -> NudgePlan:
    recent = await _get_recent_nudges(user.id)
    recent_text = "\n".join(f"- [{n.kind}] {n.topic}: {n.content[:80]}" for n in recent) if recent else "(none)"
    recent_convos = await _get_recent_conversation_summaries(user.id)

    from app.features.habits import unlogged_today
    from app.core.timeutil import now_in
    current_time = now_in(user.timezone).strftime("%A %Y-%m-%d %I:%M %p %Z")

    missing = await unlogged_today(user.id, user.timezone)
    habits_text = ", ".join(h.name for h in missing) if missing else "(all logged or none tracked)"

    llm = chat_model(fast=True, purpose="nudge_plan").with_structured_output(NudgePlan)
    return await llm.ainvoke([
        SystemMessage(NUDGE_PROMPT.format(
            name=user.name,
            current_time=current_time,
            recent_nudges=recent_text,
            recent_conversations=recent_convos,
            unlogged_habits=habits_text,
            reflection=reflection_content,
        )),
        HumanMessage("Should I send a nudge right now?"),
    ])


async def maybe_send_nudge(user, notify_fn) -> bool:
    """Decide whether to nudge and send if appropriate. Returns True if sent."""
    if not settings.nudge_enabled:
        log.debug("Nudges disabled, skipping")
        return False

    reflection = await get_latest_reflection(user.id)
    if not reflection or not reflection.content:
        log.debug("No reflection yet, skipping nudge")
        return False

    if await _was_active_recently(user.id, settings.nudge_quiet_after_convo_hours):
        log.info("Nudge skipped — user active in last %dh", settings.nudge_quiet_after_convo_hours)
        return False

    recent = await _get_recent_nudges(user.id, hours=settings.nudge_min_interval_hours)
    if recent:
        log.info("Nudge skipped — last nudge was within %dh", settings.nudge_min_interval_hours)
        return False

    plan = await plan_nudge(user, reflection.content)

    if not plan.should_send or not plan.message:
        log.info("Nudge skipped — LLM found nothing worth asking about right now")
        return False

    telegram_message_id = await notify_fn(plan.message)
    log.info("Nudge sent [%s] topic=%r msg_id=%s", plan.kind, plan.topic, telegram_message_id)

    async with session_scope() as session:
        session.add(ProactiveNudge(
            user_id=user.id,
            content=plan.message,
            topic=plan.topic,
            kind=plan.kind,
            telegram_message_id=telegram_message_id,
        ))

    return True
