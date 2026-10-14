"""After each turn, a cheap model decides what (if anything) deserves to become a long-term memory."""

import logging

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError
from sqlalchemy import select

from app.core.db import session_scope
from app.core.llm import chat_model
from app.memory.profile import refresh_profile_if_stale
from app.memory.recall import format_memories
from app.memory.schemas import Extraction, MemoryCreate
from app.memory.store import save_memory
from app.core.models import Memory
from app.obs import trace as obs
from app.core.timeutil import localize, now_in

log = logging.getLogger(__name__)

EXTRACTION_PROMPT = """You maintain the long-term memory of a personal assistant for {name}.
Current date and time: {now} (timezone {timezone}).
Read the latest exchange and save what is worth remembering for months or years.

Save: stable facts about {name} and people in their life, preferences, plans and commitments,
important events (with dates), decisions, goals, things {name} is actively working on or
looking for (job search, projects, applications), recommendations or advice {name} asked
for and received, emotional states and moods when they're clearly expressed or strongly implied
("exhausted", "excited about X", "stressed about the deadline"), and active intentions or
near-term focus areas that shape what {name} cares about right now.
Do not save: small talk, one-off questions, general knowledge that anyone could look up,
assistant-only reasoning with no user context, secrets such as passwords or card numbers.

Rules:
- `kind` must be exactly one of: fact, preference, event, episode. There is no "plan" kind:
  a plan or commitment is an `event` (with event_at) if it has a date, otherwise a `fact`.
- One self-contained sentence per memory, naming who it is about ("{name} prefers ...").
- If the exchange changes or contradicts a known memory, put that memory's id in `replaces`.
- Never repeat anything listed under KNOWN MEMORIES or SAVED THIS TURN.
- Convert relative dates ("next Friday") into real ISO dates with timezone offset in event_at.
- Return an empty list when nothing is worth saving. That is the most common correct answer."""


async def extract_memories(
    user, user_message: str, reply: str, known: list, turn_key: str, at=None
) -> list[MemoryCreate]:
    """Ask the model what is worth remembering from one exchange. Returns validated, unsaved memories.

    `at` is "now" for the prompt; re-extraction passes the turn's original time so that
    "next Friday" resolves relative to when it was said.
    """
    async with session_scope() as session:
        saved_this_turn = (await session.scalars(select(Memory).where(Memory.source_key == turn_key))).all()

    llm = chat_model(fast=True, purpose="extraction").with_structured_output(Extraction)
    result = await llm.ainvoke(
        [
            SystemMessage(
                EXTRACTION_PROMPT.format(
                    name=user.name, now=(at or now_in(user.timezone)).isoformat(), timezone=user.timezone
                )
            ),
            HumanMessage(
                f"KNOWN MEMORIES:\n{format_memories(known)}\n\n"
                f"SAVED THIS TURN:\n{format_memories(saved_this_turn)}\n\n"
                f"{user.name}: {user_message}\nAssistant: {reply}"
            ),
        ]
    )

    shown_ids = {str(m.id) for m in known}  # the model may only retire memories it was shown
    memories = []
    for item in result.memories:
        try:
            memories.append(
                MemoryCreate(
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
            )
        except ValidationError as error:
            log.warning("skipped an invalid memory from the extractor: %s", error)
    return memories


async def extract_and_save(user, user_message: str, reply: str, known: list, turn_key: str) -> None:
    """Runs in the background after the reply is sent, so it never slows the chat down."""
    trace = obs.start("extraction", "background", user_message)  # its own trace: this runs after the chat trace closed
    try:
        proposed = await extract_memories(user, user_message, reply, known, turn_key)
        saved = []
        for data in proposed:
            with obs.span("db", "save_memory", memory_kind=data.kind, importance=data.importance, text=obs.clip(data.content, 200)) as span:
                memory = await save_memory(data)
                span.attrs["memory_id"] = str(memory.id)
                span.attrs["replaces"] = len(data.replaces)
                saved.append(memory)
        if trace is not None:
            trace.meta.update(proposed=len(proposed), saved=len(saved), turn_key=turn_key)
            trace.reply_text = "; ".join(m.content for m in saved)[:1500] or "(nothing worth remembering)"
            trace.recalled = len(known)
        with obs.span("db", "refresh_profile_if_stale"):
            await refresh_profile_if_stale(user)
    except Exception as error:
        log.exception("memory extraction failed")
        if trace is not None:
            trace.fail(error)
    finally:
        await obs.finish(trace)
