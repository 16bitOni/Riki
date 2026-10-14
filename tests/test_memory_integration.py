"""Needs the Postgres from docker-compose (see README). Skipped when it is not running."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db import session_scope
from app.memory import retrieval, store
from app.memory.schemas import EntityMention, MemoryCreate, RelationMention, RetrievalPlan


def plan(**fields) -> RetrievalPlan:
    return RetrievalPlan(needs_memory=True, **fields)


def memory(user, content, **fields) -> MemoryCreate:
    return MemoryCreate(user_id=user.id, content=content, **fields)


async def test_all_four_retrieval_channels(user):
    sister = await store.save_memory(
        memory(
            user,
            "Riya is Subhadip's sister",
            entities=[EntityMention(name="Riya", type="person")],
            relations=[RelationMention(subject="Riya", predicate="sister of", target="Subhadip")],
        )
    )
    quiet = await store.save_memory(
        memory(user, "Prefers quiet cafes for deep focused work", kind="preference")
    )
    dentist = await store.save_memory(
        memory(
            user,
            "Dentist appointment on 10 March",
            kind="event",
            event_at=datetime(2031, 3, 10, 10, 0, tzinfo=timezone.utc),
        )
    )

    # exact words
    found = await retrieval.hybrid_search(user.id, plan(keywords=["Riya"]))
    assert [m.id for m in found] == [sister.id]

    # similar meaning (only embedded memories can match)
    found = await retrieval.hybrid_search(user.id, plan(semantic_query="quiet cafes for focused work"))
    assert found[0].id == quiet.id

    # time window
    window = dict(
        start=datetime(2031, 3, 9, tzinfo=timezone.utc), end=datetime(2031, 3, 11, tzinfo=timezone.utc)
    )
    found = await retrieval.hybrid_search(user.id, plan(**window))
    assert [m.id for m in found] == [dentist.id]

    # knowledge graph: the name is stored on an entity, matched case-insensitively
    found = await retrieval.hybrid_search(user.id, plan(entities=["riya"]))
    assert [m.id for m in found] == [sister.id]


async def test_other_users_memories_are_never_returned(user):
    from app.models import User

    async with session_scope() as session:
        stranger = User(name="Someone Else")
        session.add(stranger)
        await session.flush()
    try:
        await store.save_memory(MemoryCreate(user_id=stranger.id, content="Riya works at a bank"))
        assert await retrieval.hybrid_search(user.id, plan(keywords=["Riya"])) == []
    finally:
        async with session_scope() as session:
            await session.delete(await session.get(User, stranger.id))


async def test_duplicates_and_corrections(user):
    first = await store.save_memory(memory(user, "Riya is Subhadip's sister"))
    again = await store.save_memory(memory(user, "riya is subhadip's sister"))
    assert again.id == first.id  # same sentence, saved once

    old = await store.save_memory(memory(user, "Lives in Pune"))
    await store.save_memory(memory(user, "Lives in Kolkata", replaces=[old.id]))
    assert await retrieval.hybrid_search(user.id, plan(keywords=["Pune"])) == []  # old one is retired

    found = await retrieval.hybrid_search(user.id, plan(keywords=["Kolkata"]))
    assert len(found) == 1
    assert await store.forget_memory(user.id, found[0].id)
    assert await retrieval.hybrid_search(user.id, plan(keywords=["Kolkata"])) == []


async def test_database_rejects_bad_rows_even_if_validation_is_bypassed(user):
    """Second line of defence: CHECK constraints. This talks to the database with raw SQL on purpose."""
    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            await session.execute(
                text(
                    "INSERT INTO memories (id, user_id, kind, content, content_hash, importance, "
                    "created_at, updated_at) VALUES (gen_random_uuid(), :u, 'fact', 'x', 'h', 9, now(), now())"
                ),
                {"u": user.id},
            )
    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            await session.execute(
                text(
                    "INSERT INTO tasks (id, user_id, title, details, status, created_at) "
                    "VALUES (gen_random_uuid(), :u, 'x', '', 'maybe', now())"
                ),
                {"u": user.id},
            )
