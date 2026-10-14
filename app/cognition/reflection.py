"""Reflection brain: a nightly synthesis of everything Riki knows about the user.

Unlike memories (discrete facts) or session guides (how to work together), a reflection
is a living, holistic read — what's on the user's mind, what's unresolved, what patterns
exist. It gets replaced each run and injected into every conversation's system prompt.
"""

import logging
from datetime import datetime, timezone
from uuid import UUID

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import func, select

from app.core.db import session_scope
from app.core.llm import chat_model, text_of
from app.core.models import Memory, SessionGuide, UserReflection

log = logging.getLogger(__name__)

REFLECTION_PROMPT = """You are Riki, a personal assistant. Below are all the memories you hold about {name}, plus recent session notes.
Read everything and write a private, honest synthesis — your current read on this person.

This is NOT shown to the user directly. It lives in your head before each conversation.
Write like a thoughtful friend making private notes, not like a report.

Format exactly like this (keep it under 450 words total):

## Riki's current read on {name}

**Right now:** [2-3 sentences on what seems to be occupying their mind lately based on recent memories]

**Open loops:** [Specific things they mentioned but never resolved — commitments made, worries expressed, plans that went quiet. Be specific: name the thing. If nothing stands out, write "Nothing obvious."]

**Questions I'm sitting with:** [1-3 specific questions you're genuinely curious about based on what you know. NOT generic. E.g. "Did the Pune trip ever get rebooked after it was cancelled?" or "He mentioned feeling isolated in October — is that still the case?" If nothing comes to mind, write "None right now."]

**Patterns:** [Behavioral patterns you've noticed. E.g. "He sets ambitious deadlines and goes quiet when he misses them" or "Travel planning spikes when he's stressed." 2-3 sentences max.]

Rules:
- Only write what you actually know from the memories. Don't invent.
- Be specific — use real names, dates, places from the memories.
- If memories are sparse, say so honestly in the relevant section."""


async def _get_all_memories(user_id: UUID) -> list[Memory]:
    async with session_scope() as session:
        rows = (
            await session.scalars(
                select(Memory)
                .where(Memory.user_id == user_id, Memory.is_active.is_(True))
                .order_by(Memory.created_at.desc())
                .limit(200)
            )
        ).all()
    return list(rows)


async def _get_recent_guides(user_id: UUID, limit: int = 3) -> list[SessionGuide]:
    async with session_scope() as session:
        rows = (
            await session.scalars(
                select(SessionGuide)
                .where(SessionGuide.user_id == user_id)
                .order_by(SessionGuide.created_at.desc())
                .limit(limit)
            )
        ).all()
    return list(rows)


async def generate_reflection(user) -> str:
    """Generate a fresh reflection document from all memories. Returns the text."""
    memories = await _get_all_memories(user.id)
    guides = await _get_recent_guides(user.id)

    if not memories:
        return ""

    from app.memory.recall import format_memories
    memory_text = format_memories(memories)

    guide_text = ""
    if guides:
        lines = [g.learnings for g in reversed(guides) if g.learnings and g.learnings != "(no learnings)"]
        if lines:
            guide_text = "\n\nRecent session notes:\n" + "\n---\n".join(lines)

    llm = chat_model(purpose="reflection")
    reply = await llm.ainvoke([
        SystemMessage(REFLECTION_PROMPT.format(name=user.name)),
        HumanMessage(f"All memories ({len(memories)} total):\n{memory_text}{guide_text}"),
    ])
    return text_of(reply).strip()


async def get_latest_reflection(user_id: UUID) -> "UserReflection | None":
    async with session_scope() as session:
        return await session.scalar(
            select(UserReflection)
            .where(UserReflection.user_id == user_id)
            .order_by(UserReflection.created_at.desc())
            .limit(1)
        )


async def refresh_reflection_if_stale(user) -> bool:
    """Generate and save a new reflection if new memories exist since the last run.

    Returns True if a new reflection was created.
    """
    latest = await get_latest_reflection(user.id)

    async with session_scope() as session:
        query = select(func.count()).select_from(Memory).where(
            Memory.user_id == user.id,
            Memory.is_active.is_(True),
        )
        if latest is not None:
            query = query.where(Memory.created_at > latest.created_at)
        new_count = await session.scalar(query) or 0

    if new_count == 0 and latest is not None:
        log.info("Reflection up to date, skipping (no new memories since %s)", latest.created_at.strftime("%Y-%m-%d"))
        return False

    text = await generate_reflection(user)
    if not text:
        log.info("Reflection skipped — no memories yet")
        return False

    async with session_scope() as session:
        total = await session.scalar(
            select(func.count()).select_from(Memory).where(
                Memory.user_id == user.id, Memory.is_active.is_(True)
            )
        ) or 0
        session.add(UserReflection(
            user_id=user.id,
            content=text,
            memory_count=total,
        ))

    log.info("Reflection updated (%d total memories)", total)
    return True


async def get_reflection_context(user) -> str:
    """Return the latest reflection formatted for system prompt injection."""
    reflection = await get_latest_reflection(user.id)
    if not reflection or not reflection.content:
        return ""
    return f"\n\n{reflection.content}"
