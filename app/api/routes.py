"""HTTP endpoints. Kept thin: the real work lives in chat_service and the memory package.

FastAPI validates every request through the Pydantic types: ids must be UUIDs, text must be
non-empty and bounded, status must be one of the allowed words. Bad input gets a 422 and
never reaches the database.
"""

import json
import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.features import planner
from app.chat.service import run_turn, run_turn_stream
from app.config import settings
from app.core.db import get_or_create_user, session_scope
from app.memory import recall, store
from app.memory.maintenance import memory_health, reembed_stale
from app.memory.profile import refresh_profile
from app.core.models import Conversation, Message
from app.core.schemas import (
    ChatRequest,
    ChatResponse,
    ConversationOut,
    MemoryOut,
    MessageOut,
    SearchText,
    TaskOut,
    TaskStatus,
)


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if settings.api_key:
        import hmac
        if not x_api_key or not hmac.compare_digest(x_api_key, settings.api_key):
            logging.getLogger(__name__).warning("Auth failed from request (key %s)", "present" if x_api_key else "missing")
            raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest):
    user = await get_or_create_user()
    try:
        turn = await run_turn(user, body.message, body.conversation_id, source="api", images=body.images or None)
    except LookupError:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return ChatResponse(conversation_id=turn.conversation_id, reply=turn.reply)


@router.post("/chat/stream")
async def chat_stream(body: ChatRequest):
    """Server-sent events: {"token": ...} while the answer is written, then {"done": true, ...}."""
    user = await get_or_create_user()

    def event(payload: dict) -> str:
        return f"data: {json.dumps(payload)}\n\n"

    async def generate():
        try:
            async for kind, value in run_turn_stream(
                user, body.message, body.conversation_id, source="web", images=body.images or None
            ):
                if kind == "token":
                    yield event({"token": value})
                else:
                    yield event({"done": True, "conversation_id": str(value.conversation_id), "reply": value.reply})
        except LookupError:
            yield event({"error": "Conversation not found"})
        except Exception:
            logging.exception("chat stream failed")
            yield event({"error": "Something went wrong"})

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations():
    user = await get_or_create_user()
    async with session_scope() as session:
        rows = await session.scalars(
            select(Conversation)
            .where(Conversation.user_id == user.id)
            .order_by(Conversation.created_at.desc())
            .limit(50)
        )
        return rows.all()


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
async def conversation_messages(conversation_id: UUID):
    user = await get_or_create_user()
    async with session_scope() as session:
        owned = await session.scalar(
            select(Conversation.id).where(Conversation.id == conversation_id, Conversation.user_id == user.id)
        )
        if owned is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        rows = await session.scalars(
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at)
        )
        return rows.all()


@router.get("/memories", response_model=list[MemoryOut])
async def search_memories(q: Annotated[SearchText, Query()]):
    """Debug view of what the memory router would find for a message."""
    user = await get_or_create_user()
    return await recall.search(user, q)


@router.delete("/memories/{memory_id}")
async def delete_memory(memory_id: UUID):
    user = await get_or_create_user()
    if not await store.forget_memory(user.id, memory_id):
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"forgotten": memory_id}


@router.post("/profile/refresh")
async def profile_refresh():
    user = await get_or_create_user()
    return {"profile": await refresh_profile(user)}


@router.get("/tasks", response_model=list[TaskOut])
async def tasks(status: TaskStatus = "open"):
    user = await get_or_create_user()
    return await planner.list_tasks(user.id, status)


@router.get("/admin/memory-health")
async def admin_memory_health():
    """Is every memory that should be searchable actually searchable (right embedding model)?"""
    user = await get_or_create_user()
    return await memory_health(user.id)


@router.post("/admin/reembed")
async def admin_reembed(limit: Annotated[int, Query(ge=1, le=5000)] = 500):
    """Embed up to `limit` memories that are missing a vector or have one from an old model."""
    user = await get_or_create_user()
    return await reembed_stale(user.id, limit)
