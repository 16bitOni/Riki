"""Keeping the index healthy: check it, re-embed after a model change, rebuild from the archive.

Memories carry the name of the embedding model that made their vector. Search only compares
vectors from the current model, so after changing EMBEDDING_MODEL_ID old vectors are simply
ignored (never wrongly compared) until `reembed_stale` has refreshed them.
"""

import asyncio
import logging
from datetime import date, datetime, timezone
from uuid import UUID

from sqlalchemy import func, select, update

from app.config import settings
from app.core.db import session_scope
from app.core.llm import embed
from app.memory.extraction import extract_memories
from app.memory.store import save_memory
from app.core.models import Memory, User
from app.core.storage import get_archive
from app.core.timeutil import now_in

log = logging.getLogger(__name__)

BATCH = 50


def _needs_embedding():
    """A vector that needs (re)computing: wanted but missing, or made by a different model.
    A function, not a constant, so a changed EMBEDDING_MODEL_ID is picked up immediately."""
    return Memory.wants_embedding.is_(True) & (
        Memory.embedding.is_(None) | (Memory.embedding_model != settings.embedding_model_id)
    )


async def memory_health(user_id: UUID | None = None) -> dict:
    """Counts that show whether search can see all the memories that should be searchable."""
    scope = [Memory.user_id == user_id] if user_id else []
    async with session_scope() as session:
        total = await session.scalar(select(func.count()).select_from(Memory).where(*scope))
        active = await session.scalar(
            select(func.count()).select_from(Memory).where(*scope, Memory.is_active.is_(True))
        )
        pending = await session.scalar(select(func.count()).select_from(Memory).where(*scope, _needs_embedding()))
        by_model = (
            await session.execute(
                select(Memory.embedding_model, func.count())
                .where(*scope, Memory.embedding.is_not(None))
                .group_by(Memory.embedding_model)
            )
        ).all()
    models = {model: count for model, count in by_model}
    return {
        "current_model": settings.embedding_model_id,
        "memories": total,
        "active": active,
        "embedded_by_model": models,
        "needs_embedding": pending,  # missing, or made by an older model
        "healthy": pending == 0,
    }


async def reembed_stale(user_id: UUID | None = None, limit: int | None = None) -> dict:
    """Compute vectors for every memory that needs one. Safe to stop and run again.

    Each memory is updated on its own, so a crash loses nothing. A memory that fails is
    skipped for the rest of this run (so one bad row cannot loop forever) and counted.
    """
    scope = [Memory.user_id == user_id] if user_id else []
    done, failed_ids = 0, set()
    gate = asyncio.Semaphore(settings.reembed_concurrency)

    async def refresh(memory_id: UUID, content: str) -> bool:
        async with gate:
            try:
                vector = await embed(content)
            except Exception:
                log.exception("re-embedding %s failed", memory_id)
                return False
        async with session_scope() as session:
            await session.execute(
                update(Memory)
                .where(Memory.id == memory_id)
                .values(
                    embedding=vector,
                    embedding_model=settings.embedding_model_id,
                    embedded_at=datetime.now(timezone.utc),
                )
            )
        return True

    while limit is None or done + len(failed_ids) < limit:
        take = BATCH if limit is None else min(BATCH, limit - done - len(failed_ids))
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(Memory.id, Memory.content)
                    .where(*scope, _needs_embedding(), *([Memory.id.not_in(failed_ids)] if failed_ids else []))
                    .order_by(Memory.is_active.desc(), Memory.created_at.desc())  # live memories first
                    .limit(take)
                )
            ).all()
        if not rows:
            break
        outcomes = await asyncio.gather(*(refresh(i, c) for i, c in rows))
        for (memory_id, _), ok in zip(rows, outcomes):
            if ok:
                done += 1
            else:
                failed_ids.add(memory_id)
    return {"embedded": done, "failed": len(failed_ids)}


async def reextract_archive(
    user: User, since: date | None = None, limit: int | None = None, dry_run: bool = False
) -> dict:
    """Run the extraction prompt again over archived turns (after improving the prompt or model).

    Turns are replayed oldest first with their original timestamp, so "tomorrow" means the right
    day. Memories already saved are recognised (same sentence, or a near-duplicate that the newer
    wording replaces), so running this twice does not pile up copies.
    With dry_run nothing is saved; the result lists what would have been.
    """
    archive = get_archive()
    keys = await archive.list_keys(f"users/{user.id}/")
    if since:
        keys = [k for k in keys if _key_date(k) >= since]
    if limit:
        keys = keys[:limit]

    turns = proposed = saved = failed = 0
    preview: list[str] = []
    for key in keys:
        try:
            turn = await archive.get_json(key)
            at = datetime.fromisoformat(turn["at"])
            found = await extract_memories(user, turn["user"], turn["assistant"], [], key, at=at)
            turns += 1
            proposed += len(found)
            for data in found:
                if dry_run:
                    preview.append(data.content)
                else:
                    await save_memory(data)
                    saved += 1
        except Exception:
            log.exception("re-extraction failed for %s", key)
            failed += 1
    return {
        "turns": turns, "proposed": proposed, "saved": saved, "failed": failed,
        "dry_run": dry_run, "preview": preview[:20],
    }


def _key_date(key: str) -> date:
    """Archive keys look like users/{user}/{yyyy}/{mm}/{dd}/{conversation}/{uuid}.json"""
    _, _, year, month, day, *_ = key.split("/")
    return date(int(year), int(month), int(day))
