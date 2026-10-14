"""The profile is a short summary of who the user is, rebuilt from their best memories."""

from datetime import datetime, timezone

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import func, select, update

from app.core.db import session_scope
from app.core.llm import chat_model, text_of
from app.core.models import Memory, User

REFRESH_AFTER_NEW_MEMORIES = 10

PROFILE_PROMPT = """Write a profile of {name} for a personal assistant, from the memories below.
Plain prose, under 250 words, third person. Cover who they are, what they do, how they like to be helped,
the commitments or goals that matter most, and significant recent experiences that shape who they are.
Only use what the memories say. Do not invent anything."""


async def refresh_profile(user: User) -> str:
    async with session_scope() as session:
        memories = (
            await session.scalars(
                select(Memory)
                .where(Memory.user_id == user.id, Memory.is_active.is_(True))
                .order_by(Memory.importance.desc(), Memory.created_at.desc())
                .limit(80)
            )
        ).all()
    if not memories:
        return user.profile

    bullet_list = "\n".join(f"- {m.content}" for m in memories)
    reply = await chat_model(fast=True, purpose="profile").ainvoke(
        [SystemMessage(PROFILE_PROMPT.format(name=user.name)), HumanMessage(bullet_list)]
    )
    text = text_of(reply).strip()
    if not text:
        return user.profile

    async with session_scope() as session:
        await session.execute(
            update(User)
            .where(User.id == user.id)
            .values(profile=text, profile_updated_at=datetime.now(timezone.utc))
        )
    return text


async def refresh_profile_if_stale(user: User) -> None:
    since = user.profile_updated_at or datetime(1970, 1, 1, tzinfo=timezone.utc)
    async with session_scope() as session:
        new_count = await session.scalar(
            select(func.count()).select_from(Memory).where(Memory.user_id == user.id, Memory.created_at > since)
        )
    if (new_count or 0) >= REFRESH_AFTER_NEW_MEMORIES:
        await refresh_profile(user)
