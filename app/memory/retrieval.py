"""Tier 2 of memory: hybrid retrieval.

Four independent "channels" each return a ranked list of memory ids:
    keyword  - exact words (Postgres full-text search)
    semantic - similar meaning (pgvector)
    time     - inside a date window
    graph    - linked to a named person/place/project (and their direct neighbours)
The lists are merged with weighted Reciprocal Rank Fusion, so a memory found by several channels wins.
Important memories get a small boost, and an optional LLM reranker can reorder the final shortlist.
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import TypeVar
from uuid import UUID

from sqlalchemy import func, literal, literal_column, or_, select, union_all

from app.config import settings
from app.core.db import session_scope
from app.core.llm import embed
from app.memory.schemas import RetrievalPlan
from app.obs import trace as obs
from app.core.models import Entity, EntityRelation, Memory, MemoryEntity

log = logging.getLogger(__name__)

CHANNEL_DEPTH = 20  # candidates per channel before merging
MAX_TERMS = 10  # keywords or entity names taken from one plan

# A memory is dated by when it happened or, if undated, when it was saved.
_when = func.coalesce(Memory.event_at, Memory.created_at)
_english = literal_column("'english'")  # a literal, so Postgres can use the full-text index


T = TypeVar("T")


def fuse_scores(rankings: list[list[T]], weights: list[float] | None = None, k: int = 60) -> dict[T, float]:
    """Reciprocal Rank Fusion scores. A channel's `weight` scales what it contributes."""
    weights = weights or [1.0] * len(rankings)
    scores: dict[T, float] = {}
    for ranking, weight in zip(rankings, weights):
        for position, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + weight / (k + position + 1)
    return scores


def reciprocal_rank_fusion(rankings: list[list[T]], weights: list[float] | None = None, k: int = 60) -> list[T]:
    """Merge several ranked id lists into one. Higher in any list = higher overall."""
    scores = fuse_scores(rankings, weights, k)
    return sorted(scores, key=lambda item: scores[item], reverse=True)


def importance_boost(importance: int, weight: float) -> float:
    """Multiplier around 1.0: importance 3 is neutral, 5 gets +2*weight, 1 gets -2*weight."""
    return 1.0 + weight * (importance - 3)


def _live_memories(user_id: UUID, start: datetime | None, end: datetime | None) -> list:
    """WHERE clauses shared by the search channels: this user, not forgotten, inside the window."""
    clauses = [Memory.user_id == user_id, Memory.is_active.is_(True)]
    if start:
        clauses.append(_when >= start)
    if end:
        clauses.append(_when <= end)
    return clauses


async def keyword_search(user_id, keywords, start, end) -> list[UUID]:
    # each keyword becomes a quoted phrase; "or" lets any one of them match
    terms = [k.replace('"', "").strip() for k in keywords[:MAX_TERMS]]
    query = " or ".join(f'"{t}"' for t in terms if t)
    if not query:
        return []
    document = func.to_tsvector(_english, Memory.content)
    wanted = func.websearch_to_tsquery(_english, query)
    stmt = (
        select(Memory.id)
        .where(*_live_memories(user_id, start, end), document.op("@@")(wanted))
        .order_by(func.ts_rank_cd(document, wanted).desc())
        .limit(CHANNEL_DEPTH)
    )
    async with session_scope() as session:
        return list((await session.scalars(stmt)).all())


async def semantic_search(user_id, text, start, end) -> list[UUID]:
    vector = await embed(text)
    distance = Memory.embedding.cosine_distance(vector)
    stmt = (
        select(Memory.id)
        .where(
            *_live_memories(user_id, start, end),
            Memory.embedding.is_not(None),
            Memory.embedding_model == settings.embedding_model_id,  # never compare vectors from different models
            distance <= 1 - settings.min_similarity,
        )
        .order_by(distance)
        .limit(CHANNEL_DEPTH)
    )
    async with session_scope() as session:
        return list((await session.scalars(stmt)).all())


async def time_window(user_id, start, end) -> list[UUID]:
    stmt = (
        select(Memory.id)
        .where(*_live_memories(user_id, start, end))
        .order_by(Memory.importance.desc(), _when.desc())
        .limit(CHANNEL_DEPTH)
    )
    async with session_scope() as session:
        return list((await session.scalars(stmt)).all())


async def graph_search(user_id, names, relation_words=()) -> list[UUID]:
    """Memories about people/places/projects connected to the question, up to `graph_max_hops` away.

    Start points ("seeds") are the named entities plus both ends of any relation whose label
    contains one of the relationship words ("sister" finds "sister of" links). From the seeds we
    walk along relations in both directions. Memories about closer entities rank first.
    """
    wanted = [n.strip().lower() for n in list(names)[:MAX_TERMS] if n.strip()]
    words = [w.strip() for w in list(relation_words)[:MAX_TERMS] if w.strip()]
    if not wanted and not words:
        return []

    async with session_scope() as session:
        seeds: set[UUID] = set()
        if wanted:
            seeds.update(
                await session.scalars(
                    select(Entity.id).where(Entity.user_id == user_id, func.lower(Entity.name).in_(wanted))
                )
            )
        if words:
            ends = await session.execute(
                select(EntityRelation.subject_id, EntityRelation.target_id).where(
                    EntityRelation.user_id == user_id,
                    or_(*(EntityRelation.predicate.icontains(w, autoescape=True) for w in words)),
                )
            )
            for subject_id, target_id in ends:
                seeds.update([subject_id, target_id])
        if not seeds:
            return []

        # Relations can be followed in either direction ("Riya sister of Subhadip" works both ways).
        edges = union_all(
            select(EntityRelation.subject_id.label("src"), EntityRelation.target_id.label("dst")).where(
                EntityRelation.user_id == user_id
            ),
            select(EntityRelation.target_id, EntityRelation.subject_id).where(
                EntityRelation.user_id == user_id
            ),
        ).cte("edges")

        # Recursive walk: depth 0 = the seeds, depth 1 = their neighbours, and so on.
        walk = (
            select(Entity.id.label("entity_id"), literal(0).label("depth"))
            .where(Entity.id.in_(seeds))
            .cte("walk", recursive=True)
        )
        walk = walk.union(
            select(edges.c.dst, walk.c.depth + 1)
            .select_from(walk)
            .join(edges, edges.c.src == walk.c.entity_id)
            .where(walk.c.depth < settings.graph_max_hops)
        )
        closest = (
            select(walk.c.entity_id, func.min(walk.c.depth).label("depth"))
            .group_by(walk.c.entity_id)
            .order_by("depth")
            .limit(settings.graph_fanout_limit)  # a very connected graph cannot explode the query
            .subquery()
        )

        hops = func.min(closest.c.depth).label("hops")
        rows = await session.execute(
            select(Memory.id)
            .join(MemoryEntity, MemoryEntity.memory_id == Memory.id)
            .join(closest, closest.c.entity_id == MemoryEntity.entity_id)
            .where(*_live_memories(user_id, None, None))
            .group_by(Memory.id, Memory.importance, Memory.created_at)
            .order_by(hops, Memory.importance.desc(), Memory.created_at.desc())
            .limit(CHANNEL_DEPTH)
        )
        return [row[0] for row in rows]


async def _timed(name: str, coroutine):
    """Run one channel. A broken channel (say, Bedrock is down) must not break the whole recall:
    it returns no hits, and the failure is reported so the dashboard can show it."""
    t0 = time.perf_counter()
    try:
        ids, error = await coroutine, None
    except Exception as exc:
        log.exception("memory channel '%s' failed", name)
        ids, error = [], type(exc).__name__
    return name, ids, round((time.perf_counter() - t0) * 1000), error


def _channel_weights() -> dict[str, float]:
    return {
        "keyword": settings.weight_keyword,
        "semantic": settings.weight_semantic,
        "graph": settings.weight_graph,
        "time": settings.weight_time,
    }


async def hybrid_search(
    user_id: UUID,
    plan: RetrievalPlan,
    limit: int | None = None,
    rerank_query: str | None = None,
) -> list[Memory]:
    """Run every channel the plan asks for and return Memory rows, best first.

    1. channels run in parallel; a failing one is skipped
    2. weighted RRF merges them
    3. an importance prior nudges the order (multiplicative, so it can only break near-ties)
    4. optionally an LLM reranker reorders the shortlist (settings.rerank = "llm")

    Every call is recorded as a "retrieval" span (channel hits and timings, every returned memory
    with its score and which channels found it), which is what the dashboard's Retrieval tab reads.
    """
    with obs.span("retrieval", "hybrid_search") as span:
        report: dict = {}
        found = await _hybrid_search(user_id, plan, limit, rerank_query, report)
        span.attrs.update(report)
        return found


async def _hybrid_search(user_id, plan, limit, rerank_query, report: dict) -> list[Memory]:
    limit = limit or settings.recall_limit
    channels = {}
    if plan.keywords:
        channels["keyword"] = keyword_search(user_id, plan.keywords, plan.start, plan.end)
    if plan.semantic_query:
        channels["semantic"] = semantic_search(user_id, plan.semantic_query, plan.start, plan.end)
    if plan.start or plan.end:
        channels["time"] = time_window(user_id, plan.start, plan.end)
    if plan.entities or plan.relation_words:
        channels["graph"] = graph_search(user_id, plan.entities, plan.relation_words)

    outcomes = await asyncio.gather(*(_timed(name, task) for name, task in channels.items()))
    results = [ids for _, ids, _, _ in outcomes]
    weights = _channel_weights()
    report.update(
        limit=limit,
        channels={name: {"hits": len(ids), "ms": ms, "error": error, "weight": weights[name]} for name, ids, ms, error in outcomes},
        plan={
            "keywords": plan.keywords, "semantic_query": plan.semantic_query, "entities": plan.entities,
            "relation_words": plan.relation_words, "start": plan.start, "end": plan.end,
        },
        candidates=0, reranked=False, returned=0, results=[],
    )
    scores = fuse_scores(list(results), [weights[name] for name in channels])
    report["candidates"] = len(scores)
    if not scores:
        return []

    use_reranker = bool(settings.rerank == "llm" and rerank_query)
    shortlist = max(limit, settings.rerank_candidates) if use_reranker else limit * 2
    candidate_ids = sorted(scores, key=lambda i: scores[i], reverse=True)[:shortlist]

    async with session_scope() as session:
        rows = (await session.scalars(select(Memory).where(Memory.id.in_(candidate_ids)))).all()
    ranked = sorted(
        rows,
        key=lambda m: scores[m.id] * importance_boost(m.importance, settings.importance_weight),
        reverse=True,
    )
    before_rerank = [m.id for m in ranked]

    if use_reranker:
        from app.memory.rerank import rerank  # imported here so the LLM stack is only needed when enabled

        ranked = await rerank(rerank_query or "", ranked)
        report["reranked"] = True
    final = ranked[:limit]

    positions = {name: {mid: i + 1 for i, mid in enumerate(ids)} for name, ids, _, _ in outcomes}
    report["returned"] = len(final)
    report["results"] = [
        {
            "id": str(m.id), "rank": rank, "kind": m.kind, "importance": m.importance,
            "rrf": round(scores[m.id], 5), "score": round(scores[m.id] * importance_boost(m.importance, settings.importance_weight), 5),
            "found_by": {name: pos[m.id] for name, pos in positions.items() if m.id in pos},
            "moved_by_rerank": before_rerank.index(m.id) + 1 - rank if use_reranker else 0,
            "text": obs.clip(m.content, 160),
        }
        for rank, m in enumerate(final, start=1)
    ]
    return final
