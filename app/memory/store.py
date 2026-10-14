"""Writing to long-term memory: a Postgres row, sometimes an embedding, and graph links.

Everything goes through `save_memory(MemoryCreate)`, so every write has been validated.
"""

import hashlib
import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.db import session_scope
from app.core.llm import embed
from app.memory.schemas import EntityMention, MemoryCreate, RelationMention
from app.core.models import Entity, EntityRelation, Memory, MemoryEntity

log = logging.getLogger(__name__)

# Meaning-search is only worth paying for on these kinds, unless the caller says otherwise.
EMBED_BY_DEFAULT = {"preference", "episode"}


async def _get_or_create_entity(
    session: AsyncSession, user_id: UUID, name: str, type_: str = "other"
) -> Entity:
    found = await session.scalar(
        select(Entity).where(Entity.user_id == user_id, func.lower(Entity.name) == name.lower())
    )
    if found:
        return found
    entity = Entity(user_id=user_id, name=name, type=type_)
    session.add(entity)
    await session.flush()
    return entity


async def _link_entities(
    session: AsyncSession,
    user_id: UUID,
    memory_id: UUID,
    entities: list[EntityMention],
    relations: list[RelationMention],
) -> None:
    entity_ids: set[UUID] = set()
    for mention in entities:
        entity = await _get_or_create_entity(session, user_id, mention.name, mention.type)
        entity_ids.add(entity.id)

    for rel in relations:
        subject = await _get_or_create_entity(session, user_id, rel.subject)
        target = await _get_or_create_entity(session, user_id, rel.target)
        entity_ids.update([subject.id, target.id])
        exists = await session.scalar(
            select(EntityRelation.id).where(
                EntityRelation.subject_id == subject.id,
                EntityRelation.predicate == rel.predicate,
                EntityRelation.target_id == target.id,
            )
        )
        if not exists:
            session.add(
                EntityRelation(
                    user_id=user_id, subject_id=subject.id, predicate=rel.predicate, target_id=target.id
                )
            )

    session.add_all(MemoryEntity(memory_id=memory_id, entity_id=eid) for eid in entity_ids)


async def _find_near_duplicate(session: AsyncSession, data: MemoryCreate, vector: list[float] | None):
    """An existing memory that says (almost) the same thing, or None.

    A candidate must be this user's, still active, the same kind, and about the same date
    (so "dentist on 10 March" is never merged with "dentist on 12 April").
    Two checks: similar spelling (pg_trgm, for every memory), then similar meaning (vectors, only
    for memories that have one and were made by the current embedding model).
    """
    same_slot = [
        Memory.user_id == data.user_id,
        Memory.is_active.is_(True),
        Memory.kind == data.kind,
        Memory.event_at.is_not_distinct_from(data.event_at),
    ]

    await session.execute(
        select(func.set_config("pg_trgm.similarity_threshold", str(settings.dedup_trigram_threshold), True))
    )
    similar_spelling = await session.scalar(
        select(Memory)
        .where(*same_slot, Memory.content.op("%")(data.content))
        .order_by(func.similarity(Memory.content, data.content).desc())
        .limit(1)
    )
    if similar_spelling or vector is None:
        return similar_spelling

    distance = Memory.embedding.cosine_distance(vector)
    return await session.scalar(
        select(Memory)
        .where(
            *same_slot,
            Memory.embedding.is_not(None),
            Memory.embedding_model == settings.embedding_model_id,
            distance <= 1 - settings.dedup_vector_threshold,
        )
        .order_by(distance)
        .limit(1)
    )


async def save_memory(data: MemoryCreate) -> Memory:
    """Save one memory and return it.

    - The same sentence twice returns the first one.
    - A near-duplicate (same thing, worded differently) is replaced by the newer wording; the older
      row is retired, not deleted, and the higher importance of the two is kept.
    - If the embedding fails, the memory is still saved and flagged so `reembed` can finish the job.
    """
    content_hash = hashlib.sha256(data.content.lower().encode("utf-8")).hexdigest()

    # 1. Exact duplicate? Answer before paying for an embedding.
    async with session_scope() as session:
        existing = await session.scalar(
            select(Memory).where(Memory.user_id == data.user_id, Memory.content_hash == content_hash)
        )
        if existing:
            existing.is_active = True  # saying it again after "forget" brings it back
            return existing

    # 2. Embed outside any transaction (a slow network call must not hold the database).
    wants_vector = data.semantic if data.semantic is not None else data.kind in EMBED_BY_DEFAULT
    vector = None
    if wants_vector:
        try:
            vector = await embed(data.content)
        except Exception:
            log.exception("could not embed a new memory; it is saved and will be picked up by reembed")

    # 3. Write everything in one transaction.
    async with session_scope() as session:
        near = await _find_near_duplicate(session, data, vector)
        memory = Memory(
            user_id=data.user_id,
            kind=data.kind,
            content=data.content,
            content_hash=content_hash,
            importance=max(data.importance, near.importance) if near else data.importance,
            event_at=data.event_at,
            source_key=data.source_key,
            wants_embedding=wants_vector,
            embedding=vector,
            embedding_model=settings.embedding_model_id if vector is not None else None,
            embedded_at=datetime.now(timezone.utc) if vector is not None else None,
        )
        session.add(memory)
        await session.flush()

        await _link_entities(session, data.user_id, memory.id, data.entities, data.relations)

        retire = set(data.replaces)
        if near:
            log.debug("memory %s replaces near-duplicate %s", memory.id, near.id)
            retire.add(near.id)
        if retire:
            await session.execute(
                update(Memory)
                .where(Memory.id.in_(retire), Memory.user_id == data.user_id, Memory.id != memory.id)
                .values(is_active=False, superseded_by_id=memory.id)
            )
    return memory


async def forget_memory(user_id: UUID, memory_id: UUID) -> bool:
    """Soft delete: the row stays for audit but is never retrieved again."""
    async with session_scope() as session:
        result = await session.execute(
            update(Memory)
            .where(Memory.id == memory_id, Memory.user_id == user_id)
            .values(is_active=False)
        )
        return result.rowcount > 0
