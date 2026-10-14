"""Session lifecycle: reflection, extraction, and learning at session end.

A "session" is a continuous conversation. It ends when:
- the user says /new
- inactivity exceeds session_timeout_hours
- the server shuts down gracefully
- the server restarts (crash recovery picks up unfinished sessions)

At session end, two things happen:
1. Personal extraction — extract facts/preferences/events from the full conversation (replaces per-turn)
2. Self-learning — extract operational lessons about how to work with this user

Both run once with the complete session context, which is more accurate than per-turn extraction.
"""

import logging
from uuid import UUID

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select

from app.config import settings
from app.core.db import session_scope
from app.core.llm import chat_model, text_of
from app.memory.profile import refresh_profile_if_stale
from app.memory.recall import format_memories
from app.memory.store import save_memory
from app.core.models import Conversation, Message, SessionGuide
from app.obs import trace as obs

log = logging.getLogger(__name__)

MIN_MESSAGES_FOR_EXTRACTION = 4  # skip sessions that are just "hi" / "yo"
MIN_MESSAGES_FOR_GUIDE = 6  # need a real conversation to learn from

SELF_LEARNING_PROMPT = """You just finished a conversation session as a personal assistant named Riki for {name}.
Review the conversation below and write a concise self-guide for your future self.

Focus on:
- Corrections the user made to your behavior or approach (most important)
- Approaches that worked well and the user was happy with
- How the user prefers to communicate (brevity, tone, level of detail)
- Task patterns: what the user commonly asks for and the best way to handle it
- Anything the user explicitly or implicitly expects you to remember about how to work together

Do NOT include:
- Facts about the user's life (those are extracted separately)
- What happened in the conversation (this is a guide, not a summary)
- Generic advice ("be helpful", "be concise" — only specific, earned lessons)

Write in second person ("you should...", "when the user asks X, do Y...").
Keep it under 300 words. If the conversation was too short or routine to learn anything from, say "Nothing new to learn from this session." and stop."""

SESSION_EXTRACTION_PROMPT = """You maintain the long-term memory of a personal assistant for {name}.
Current date and time: {now} (timezone {timezone}).

Read the ENTIRE conversation below (not just one exchange) and save what is worth remembering for months or years.
You have the full picture of the session, so you can see how discussions evolved, what the user decided in the end,
and which earlier statements were corrected or changed.

Save: stable facts about {name} and people in their life, preferences, plans and commitments,
important events (with dates), decisions, goals.
Do not save: small talk, one-off questions, general knowledge, things only the assistant said,
secrets such as passwords or card numbers.

Rules:
- `kind` must be exactly one of: fact, preference, event, episode.
  A plan or commitment is an `event` (with event_at) if it has a date, otherwise a `fact`.
- One self-contained sentence per memory, naming who it is about ("{name} prefers ...").
- If the user changed their mind during the session, only save the final state.
- Never repeat anything listed under KNOWN MEMORIES.
- Convert relative dates into real ISO dates with timezone offset in event_at.
- Return an empty list when nothing is worth saving. That is the most common correct answer."""


async def _get_session_messages(conversation_id: UUID) -> list[tuple[str, str]]:
    """Load all messages from a conversation, oldest first."""
    async with session_scope() as session:
        rows = (
            await session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at)
            )
        ).all()
    return [(row.role, row.content) for row in rows]


async def _already_extracted(conversation_id: UUID) -> bool:
    """Check if a session guide already exists for this conversation."""
    async with session_scope() as session:
        return await session.scalar(
            select(SessionGuide.id).where(SessionGuide.conversation_id == conversation_id)
        ) is not None


async def extract_session_memories(user, conversation_id: UUID) -> int:
    """Extract personal memories from the full session. Returns count of memories saved."""
    messages = await _get_session_messages(conversation_id)
    if len(messages) < MIN_MESSAGES_FOR_EXTRACTION:
        log.debug("Skipping extraction for %s (%d msgs < %d)", conversation_id, len(messages), MIN_MESSAGES_FOR_EXTRACTION)
        return 0

    conversation_text = "\n".join(
        f"{user.name if role == 'user' else 'Riki'}: {content}" for role, content in messages
    )
    turn_key = f"session/{conversation_id}"

    from app.memory.schemas import Extraction, MemoryCreate
    from app.memory.recall import search
    from app.core.timeutil import localize, now_in

    known = await search(user, conversation_text[:500])

    llm = chat_model(fast=True, purpose="session_extraction").with_structured_output(Extraction)
    result = await llm.ainvoke([
        SystemMessage(SESSION_EXTRACTION_PROMPT.format(
            name=user.name, now=now_in(user.timezone).isoformat(), timezone=user.timezone
        )),
        HumanMessage(
            f"KNOWN MEMORIES:\n{format_memories(known)}\n\nCONVERSATION:\n{conversation_text}"
        ),
    ])

    shown_ids = {str(m.id) for m in known}
    saved = 0
    for item in result.memories:
        try:
            data = MemoryCreate(
                user_id=user.id,
                content=item.content,
                kind=item.kind,
                importance=item.importance,
                event_at=localize(item.event_at, user.timezone),
                semantic=item.semantic,
                source_key=turn_key,
                entities=item.entities,
                relations=item.relations,
                replaces=[r for r in item.replaces if r in shown_ids],
            )
            await save_memory(data)
            saved += 1
        except Exception:
            log.warning("skipped invalid memory during session extraction", exc_info=True)
    return saved


async def generate_self_learning(user, conversation_id: UUID) -> str | None:
    """Generate operational lessons from the session. Returns the guide text or None."""
    messages = await _get_session_messages(conversation_id)
    if len(messages) < MIN_MESSAGES_FOR_GUIDE:
        log.debug("Skipping self-learning for %s (%d msgs < %d)", conversation_id, len(messages), MIN_MESSAGES_FOR_GUIDE)
        return None

    conversation_text = "\n".join(
        f"{user.name if role == 'user' else 'Riki'}: {content}" for role, content in messages
    )

    llm = chat_model(purpose="self_learning")
    reply = await llm.ainvoke([
        SystemMessage(SELF_LEARNING_PROMPT.format(name=user.name)),
        HumanMessage(conversation_text),
    ])
    text = text_of(reply).strip()
    if not text or "nothing new to learn" in text.lower():
        return None
    return text


async def close_session(user, conversation_id: UUID) -> dict:
    """Run all session-end processing for a conversation. Safe to call multiple times."""
    short_id = str(conversation_id)[:8]
    if await _already_extracted(conversation_id):
        log.debug("Session %s… already closed, skipping", short_id)
        return {"skipped": True, "reason": "already extracted"}

    log.info("Closing session %s…", short_id)
    trace = obs.start("session_close", "background", f"closing {conversation_id}", conversation_id)
    result = {"conversation_id": str(conversation_id), "memories_saved": 0, "guide_saved": False}
    try:
        memories_saved = await extract_session_memories(user, conversation_id)
        result["memories_saved"] = memories_saved

        guide_text = await generate_self_learning(user, conversation_id)
        messages = await _get_session_messages(conversation_id)
        async with session_scope() as session:
            session.add(SessionGuide(
                user_id=user.id,
                conversation_id=conversation_id,
                learnings=guide_text or "(no learnings)",
                message_count=len(messages),
            ))
        result["guide_saved"] = bool(guide_text)

        await refresh_profile_if_stale(user)

        log.info(
            "Session %s… closed  memories=%d  guide=%s",
            short_id, memories_saved, "saved" if result["guide_saved"] else "skipped",
        )
        if trace is not None:
            trace.meta.update(result)
    except Exception as error:
        log.error("Session %s… close failed: %s", short_id, error)
        if trace is not None:
            trace.fail(error)
    finally:
        await obs.finish(trace)
    return result


async def get_session_guide_context(user) -> str:
    """Load recent session guides and format them for injection into the system prompt."""
    async with session_scope() as session:
        guides = (
            await session.scalars(
                select(SessionGuide)
                .where(SessionGuide.user_id == user.id)
                .order_by(SessionGuide.created_at.desc())
                .limit(settings.session_guide_limit)
            )
        ).all()
    lines = []
    for g in reversed(guides):  # oldest first
        if g.learnings and g.learnings != "(no learnings)":
            lines.append(f"- [{g.created_at.strftime('%Y-%m-%d')}] {g.learnings}")
    if not lines:
        return ""
    return f"\n\n## Lessons from past sessions\n" + "\n".join(lines)
