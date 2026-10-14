"""Tools for the memory keeper agent.

Argument types (MemoryText, Importance, UUID ...) are checked by Pydantic before the function runs.
If the model sends something invalid it gets a clear error back and can try again.
"""

from datetime import datetime
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.memory import recall, store
from app.memory.schemas import MemoryCreate
from app.core.schemas import Importance, MemoryKind, MemoryText, SearchText
from app.core.timeutil import localize


@tool
async def remember(
    content: MemoryText,
    kind: MemoryKind = "fact",
    importance: Importance = 3,
    event_date: datetime | None = None,
    *,
    config: RunnableConfig,
) -> str:
    """Save something about the user to long-term memory.

    content: one self-contained sentence, e.g. "Subhadip's sister is called Riya".
    kind: fact (stable truth), preference (likes/dislikes/how they want things), event (dated happening), episode (summary of a period or conversation).
    importance: 1 (trivia) to 5 (core to who they are).
    event_date: ISO 8601 date/time if the memory is tied to a date, otherwise omit.
    """
    ctx = config["configurable"]
    user = ctx["user"]
    memory = await store.save_memory(
        MemoryCreate(
            user_id=user.id,
            content=content,
            kind=kind,
            importance=importance,
            event_at=localize(event_date, user.timezone),
            source_key=ctx.get("turn_key"),
        )
    )
    return f"Saved (id: {memory.id})."


@tool
async def search_memory(query: SearchText, *, config: RunnableConfig) -> str:
    """Search the user's long-term memory. Use a natural-language question or topic,
    e.g. "what did I decide about the Pune trip" or "my sister's birthday"."""
    user = config["configurable"]["user"]
    memories = await recall.search(user, query)
    return recall.format_memories(memories)


@tool
async def forget_memory(memory_id: UUID, *, config: RunnableConfig) -> str:
    """Forget one memory by its id (ids are shown in search_memory results).
    Only call this when the user asks to forget or correct something."""
    user = config["configurable"]["user"]
    done = await store.forget_memory(user.id, memory_id)
    return "Forgotten." if done else "No memory with that id."
