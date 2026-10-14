"""Attention threads: explicit tracked hypotheses the agent watches across cognition cycles.

Each thread represents something the agent noticed and is actively tracking — a pattern
forming, an open loop aging, a mood shift developing. Threads are first-class DB objects
with a lifecycle: watching → resolved or abandoned.

The monologue reasons *about* threads; threads are the durable structure underneath.
"""

import logging
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from app.config import settings
from app.core.db import session_scope
from app.core.models import AttentionThread, ProactiveNudge

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- LLM output models


class ThreadAssessment(BaseModel):
    thread_id: str = Field(description="UUID of existing thread, or 'new' to create")
    action: Literal["add_evidence", "resolve", "create", "no_change"]
    observation: str = Field(default="", description="For add_evidence: what was observed")
    resolution: str = Field(default="", description="For resolve: what happened")
    was_accurate: bool | None = Field(default=None, description="For resolve: was the hypothesis correct")
    topic: str = Field(default="", description="For create: thread topic")
    hypothesis: str = Field(default="", description="For create: what you think is happening")
    trigger: str = Field(default="", description="For create: when to act on this")
    confidence: float = Field(default=0.5, ge=0, le=1, description="For create: how sure you are")


# ---------------------------------------------------------------- CRUD


async def create_thread(
    user_id: UUID,
    topic: str,
    hypothesis: str,
    trigger: str = "",
    confidence: float = 0.5,
    source_cycle_id: UUID | None = None,
) -> AttentionThread | None:
    async with session_scope() as session:
        active_count = await session.scalar(
            select(func.count()).select_from(AttentionThread).where(
                AttentionThread.user_id == user_id,
                AttentionThread.status == "watching",
            )
        ) or 0
        if active_count >= settings.thread_max_active:
            log.warning(
                "Cannot create thread %r — already at max (%d/%d)",
                topic, active_count, settings.thread_max_active,
            )
            return None

        thread = AttentionThread(
            user_id=user_id,
            topic=topic,
            hypothesis=hypothesis,
            trigger=trigger,
            confidence=confidence,
            source_cycle_id=source_cycle_id,
        )
        session.add(thread)
        await session.flush()
        log.info("Created attention thread %s: %r", thread.id, topic)
        return thread


async def add_evidence(
    thread_id: UUID, cycle_id: UUID, observation: str
) -> AttentionThread | None:
    async with session_scope() as session:
        thread = await session.scalar(
            select(AttentionThread).where(AttentionThread.id == thread_id)
        )
        if not thread or thread.status != "watching":
            log.warning("Cannot add evidence to thread %s (not found or not watching)", thread_id)
            return None

        entry = {
            "cycle_id": str(cycle_id),
            "observation": observation,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        thread.evidence = [*thread.evidence, entry]
        thread.cycles_without_update = 0
        thread.last_updated_at = datetime.now(timezone.utc)
        return thread


async def resolve_thread(
    thread_id: UUID, resolution: str, was_accurate: bool | None = None
) -> AttentionThread | None:
    async with session_scope() as session:
        thread = await session.scalar(
            select(AttentionThread).where(AttentionThread.id == thread_id)
        )
        if not thread:
            log.warning("Cannot resolve thread %s — not found", thread_id)
            return None

        thread.status = "resolved"
        thread.resolution = resolution
        thread.was_accurate = was_accurate
        thread.last_updated_at = datetime.now(timezone.utc)
        log.info("Resolved thread %s (%r): %s", thread_id, thread.topic, resolution[:80])
        return thread


async def abandon_stale_threads(user_id: UUID) -> list[AttentionThread]:
    async with session_scope() as session:
        stale = list((await session.scalars(
            select(AttentionThread).where(
                AttentionThread.user_id == user_id,
                AttentionThread.status == "watching",
                AttentionThread.cycles_without_update >= AttentionThread.resolve_after_cycles,
            )
        )).all())

        now = datetime.now(timezone.utc)
        for thread in stale:
            thread.status = "abandoned"
            thread.resolution = (
                f"auto-abandoned after {thread.cycles_without_update} cycles without evidence"
            )
            thread.last_updated_at = now
            log.info("Auto-abandoned thread %s: %r", thread.id, thread.topic)

        return stale


async def increment_stale_counters(user_id: UUID, updated_thread_ids: set[UUID]) -> None:
    async with session_scope() as session:
        stmt = (
            update(AttentionThread)
            .where(
                AttentionThread.user_id == user_id,
                AttentionThread.status == "watching",
            )
            .values(cycles_without_update=AttentionThread.cycles_without_update + 1)
        )
        if updated_thread_ids:
            stmt = stmt.where(AttentionThread.id.notin_(updated_thread_ids))
        await session.execute(stmt)


# ---------------------------------------------------------------- queries


async def get_active_threads(user_id: UUID) -> list[AttentionThread]:
    async with session_scope() as session:
        rows = (await session.scalars(
            select(AttentionThread)
            .where(AttentionThread.user_id == user_id, AttentionThread.status == "watching")
            .order_by(AttentionThread.created_at.desc())
        )).all()
    return list(rows)


async def get_thread_accuracy_stats(user_id: UUID) -> dict:
    async with session_scope() as session:
        resolved_with_verdict = list((await session.scalars(
            select(AttentionThread).where(
                AttentionThread.user_id == user_id,
                AttentionThread.status == "resolved",
                AttentionThread.was_accurate.isnot(None),
            )
        )).all())

        total_resolved = await session.scalar(
            select(func.count()).select_from(AttentionThread).where(
                AttentionThread.user_id == user_id,
                AttentionThread.status == "resolved",
            )
        ) or 0

        total_reached_out = await session.scalar(
            select(func.count()).select_from(ProactiveNudge).where(
                ProactiveNudge.user_id == user_id,
                ProactiveNudge.attention_thread_id.isnot(None),
            )
        ) or 0

    accurate = sum(1 for t in resolved_with_verdict if t.was_accurate)
    inaccurate = sum(1 for t in resolved_with_verdict if not t.was_accurate)
    denominator = accurate + inaccurate

    return {
        "total_resolved": total_resolved,
        "accurate": accurate,
        "inaccurate": inaccurate,
        "accuracy_rate": accurate / denominator if denominator > 0 else None,
        "total_reached_out": total_reached_out,
    }


# ---------------------------------------------------------------- formatting


def format_threads_for_prompt(threads: list[AttentionThread]) -> str:
    if not threads:
        return "(no active attention threads)"

    parts = []
    for i, t in enumerate(threads, 1):
        age_cycles = t.cycles_without_update + len(t.evidence) if isinstance(t.evidence, list) else 0
        lines = [
            f"[{i}] {t.topic} (confidence: {t.confidence:.1f}, watching for {age_cycles} cycles)",
        ]
        if t.hypothesis:
            lines.append(f"  Hypothesis: {t.hypothesis}")
        if t.trigger:
            lines.append(f"  Trigger: {t.trigger}")
        evidence_list = t.evidence if isinstance(t.evidence, list) else []
        if evidence_list:
            lines.append("  Evidence:")
            for e in evidence_list[-5:]:
                ts = e.get("timestamp", "?")[:10]
                lines.append(f"  - [{ts}] {e.get('observation', '')}")
        parts.append("\n".join(lines))

    return "\n\n".join(parts)
