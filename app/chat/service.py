"""One chat turn from start to finish.

With checkpointing enabled, the graph persists its full state (all messages including tool
calls and images) between turns. Only the new user message is sent as
input; the checkpointer restores the previous state automatically.

When no checkpoint exists (new conversation or tests without a checkpointer), the service
falls back to loading recent messages from the database.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from uuid import UUID

from langchain_core.messages import HumanMessage
from sqlalchemy import func, select

from app.agents.graph import get_graph
from app.core.checkpoint import get_checkpointer
from app.config import settings
from app.core.db import session_scope
from app.core.llm import text_of
from app.core.models import Conversation, Message
from datetime import datetime, timezone as tz
from app.obs import trace as obs
from app.obs.callbacks import handler as obs_handler
from app.core.schemas import ChatText, ImageInput
from app.core.storage import get_archive
from app.core.timeutil import now_in

log = logging.getLogger(__name__)


@dataclass
class TurnResult:
    conversation_id: UUID
    reply: str
    turn_key: str
    recalled: list
    images: list[str] = field(default_factory=list)  # picture URLs the agent found this turn


def final_reply(messages) -> str:
    """The agent's final text — the last non-empty AI message without tool calls."""
    for m in reversed(messages):
        if m.type == "ai" and not getattr(m, "tool_calls", None):
            t = text_of(m).strip()
            if t:
                return t
    return "(no reply)"


def _build_user_message(text: str, images: list[ImageInput] | None = None) -> HumanMessage | tuple[str, str]:
    """Build a HumanMessage with text + optional images, or a plain tuple when there are no images."""
    if not images:
        return ("user", text)
    content: list[dict] = [{"type": "text", "text": text}]
    for img in images:
        if img.base64:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{img.media_type};base64,{img.base64}"},
            })
        elif img.url:
            content.append({"type": "image_url", "image_url": {"url": img.url}})
    return HumanMessage(content=content)


async def _prepare(user, message: ChatText, conversation_id: UUID | None, images: list[ImageInput] | None = None):
    has_checkpointer = get_checkpointer() is not None

    async with session_scope() as session:
        if conversation_id:
            conversation = await session.scalar(
                select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user.id)
            )
            if conversation is None:
                raise LookupError("conversation not found")
        else:
            conversation = Conversation(user_id=user.id, title=message[:60])
            session.add(conversation)
            await session.flush()

        if has_checkpointer:
            history = []
        else:
            rows = (
                await session.scalars(
                    select(Message)
                    .where(Message.conversation_id == conversation.id)
                    .order_by(Message.created_at.desc())
                    .limit(settings.history_messages)
                )
            ).all()
            history = [(row.role, row.content) for row in reversed(rows)]

        last_message_at = await session.scalar(
            select(func.max(Message.created_at)).where(Message.conversation_id == conversation.id)
        )

    now = now_in(user.timezone)
    turn_key = f"users/{user.id}/{now:%Y/%m/%d}/{conversation.id}/{uuid.uuid4()}.json"
    user_msg = _build_user_message(message, images)
    inputs = {"messages": [*history, user_msg]}

    config = {
        "configurable": {
            "user": user,
            "turn_key": turn_key,
            "images": [],
            "thread_id": str(conversation.id),
            "last_message_at": last_message_at,
        },
        "recursion_limit": 30,
        "callbacks": [obs_handler],
    }
    return conversation, now, turn_key, inputs, config


async def _finish(conversation, now, turn_key, message, reply, recalled, images=()) -> TurnResult:
    async with session_scope() as session:
        session.add(Message(conversation_id=conversation.id, role="user", content=message))
        session.add(Message(conversation_id=conversation.id, role="assistant", content=reply))

    # Tier 1: the permanent, immutable copy of this turn.
    await get_archive().put_json(
        turn_key,
        {
            "conversation_id": str(conversation.id),
            "at": now.isoformat(),
            "user": message,
            "assistant": reply,
            "recalled_memory_ids": [str(mid) for mid in recalled],
        },
    )
    return TurnResult(conversation.id, reply, turn_key, recalled, list(images))


def _note_turn(trace, conversation, turn_key, reply, recalled) -> None:
    if trace is not None:
        trace.conversation_id = conversation.id
        trace.reply_text = reply
        trace.recalled = len(recalled)
        trace.meta["turn_key"] = turn_key


async def run_turn(
    user, message: ChatText, conversation_id: UUID | None = None, source: str = "api",
    images: list[ImageInput] | None = None,
) -> TurnResult:
    trace = obs.start("chat", source, message, conversation_id)
    t0 = time.monotonic()
    log.info("[%s] Turn start  len=%d", source, len(message))
    try:
        conversation, now, turn_key, inputs, config = await _prepare(user, message, conversation_id, images)
        result = await get_graph().ainvoke(inputs, config=config)
        reply = final_reply(result["messages"])
        elapsed = time.monotonic() - t0
        log.info(
            "[%s] Turn done   conv=%s  %.1fs  reply=%d chars",
            source, str(conversation.id)[:8], elapsed, len(reply),
        )
        _note_turn(trace, conversation, turn_key, reply, result.get("recalled", []))
        return await _finish(
            conversation, now, turn_key, message, reply, result.get("recalled", []), config["configurable"]["images"]
        )
    except BaseException as error:
        elapsed = time.monotonic() - t0
        if "toolResult" in str(error) and "Expected" in str(error):
            log.error("[%s] Turn failed  %.1fs  checkpoint corruption (unmatched tool call)", source, elapsed)
        else:
            log.error("[%s] Turn failed  %.1fs  %s", source, elapsed, type(error).__name__)
        if trace is not None:
            trace.fail(error)
        raise
    finally:
        await asyncio.shield(obs.finish(trace))


async def run_turn_stream(
    user, message: ChatText, conversation_id: UUID | None = None, source: str = "web",
    images: list[ImageInput] | None = None,
):
    trace = obs.start("chat", source, message, conversation_id)
    log.info("[%s] Stream start  len=%d", source, len(message))
    try:
        async for item in _stream(user, message, conversation_id, trace, images):
            yield item
    except BaseException as error:
        if trace is not None:
            trace.fail("client disconnected" if isinstance(error, GeneratorExit) else error)
        raise
    finally:
        await asyncio.shield(obs.finish(trace))


async def _stream(user, message: ChatText, conversation_id: UUID | None, trace, images: list[ImageInput] | None = None):
    """Yield ("token", text) while the agent writes its answer, then ("done", TurnResult).

    Tool-call chunks are skipped; only final text tokens are streamed.
    """
    conversation, now, turn_key, inputs, config = await _prepare(user, message, conversation_id, images)
    final = None
    async for namespace, mode, data in get_graph().astream(
        inputs, config=config, stream_mode=["messages", "values"], subgraphs=True
    ):
        if mode == "values":
            if not namespace:
                final = data
            continue
        chunk, _ = data
        if getattr(chunk, "tool_call_chunks", None):
            continue
        if chunk.type != "AIMessageChunk":
            continue
        piece = text_of(chunk)
        if piece:
            yield "token", piece
    reply = final_reply(final["messages"])
    _note_turn(trace, conversation, turn_key, reply, final.get("recalled", []))
    yield "done", await _finish(
        conversation, now, turn_key, message, reply, final.get("recalled", []), config["configurable"]["images"]
    )


async def get_conversation_message_count(conversation_id: UUID) -> int:
    """How many messages are in a conversation (for session-end extraction)."""
    async with session_scope() as session:
        return await session.scalar(
            select(func.count()).select_from(Message).where(Message.conversation_id == conversation_id)
        ) or 0
