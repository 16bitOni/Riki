"""Multi-hop graph, near-duplicate detection, embedding drift, maintenance. Real Postgres, fake embeddings."""

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text

from app.config import settings
from app.db import assert_embedding_dimension, session_scope
from app.memory import maintenance, retrieval, store
from app.memory.evaluation import EvalCase, score_case
from app.memory.rerank import apply_order
from app.memory.schemas import EntityMention, MemoryCreate, RelationMention, RetrievalPlan
from app.models import Memory
from app.storage import LocalArchive
from tests.fakes import fake_embed


def plan(**fields) -> RetrievalPlan:
    return RetrievalPlan(needs_memory=True, **fields)


def memory(user, content, **fields) -> MemoryCreate:
    return MemoryCreate(user_id=user.id, content=content, **fields)


def link(subject, predicate, target):
    return RelationMention(subject=subject, predicate=predicate, target=target)


async def active_contents(user) -> list[str]:
    async with session_scope() as session:
        rows = await session.scalars(
            select(Memory.content).where(Memory.user_id == user.id, Memory.is_active.is_(True))
        )
        return sorted(rows.all())


# ---------- multi-hop graph ----------

async def build_family(user):
    await store.save_memory(memory(user, "Riya is Subhadip's sister", relations=[link("Riya", "sister of", "Subhadip")]))
    await store.save_memory(memory(user, "Rahul is married to Riya", relations=[link("Rahul", "husband of", "Riya")]))
    return await store.save_memory(
        memory(user, "Rahul works at Acme", relations=[link("Rahul", "works at", "Acme")])
    )


async def test_graph_follows_several_hops(user, monkeypatch):
    acme = await build_family(user)  # Subhadip - Riya - Rahul - Acme

    monkeypatch.setattr(settings, "graph_max_hops", 1)
    found = await retrieval.graph_search(user.id, ["Subhadip"])
    assert acme.id not in found  # Rahul is two hops away

    monkeypatch.setattr(settings, "graph_max_hops", 2)
    found = await retrieval.graph_search(user.id, ["Subhadip"])
    assert acme.id in found


async def test_graph_ranks_closer_memories_first(user):
    await build_family(user)
    found = await retrieval.hybrid_search(user.id, plan(entities=["Subhadip"]))
    assert [m.content for m in found][0] == "Riya is Subhadip's sister"


async def test_graph_can_start_from_a_relationship_word(user):
    acme = await build_family(user)
    found = await retrieval.hybrid_search(user.id, plan(relation_words=["husband"]))
    assert acme.id in [m.id for m in found]  # no name needed: "husband of" seeds the walk


async def test_graph_ignores_other_users_edges(user):
    from app.models import User

    async with session_scope() as session:
        stranger = User(name="Someone Else")
        session.add(stranger)
        await session.flush()
    try:
        await store.save_memory(
            MemoryCreate(user_id=stranger.id, content="Subhadip knows Bob", relations=[link("Subhadip", "knows", "Bob")])
        )
        assert await retrieval.graph_search(user.id, ["Subhadip"]) == []
    finally:
        async with session_scope() as session:
            await session.delete(await session.get(User, stranger.id))


# ---------- near-duplicates ----------

async def test_trigram_near_duplicate_is_merged_newer_wins(user):
    first = await store.save_memory(memory(user, "Prefers quiet cafes for deep focused work", kind="preference", importance=4))
    second = await store.save_memory(memory(user, "Prefers quiet cafes for deep focused work.", kind="preference", importance=2))
    assert second.id != first.id
    assert await active_contents(user) == ["Prefers quiet cafes for deep focused work."]
    assert second.importance == 4  # the higher importance survives


async def test_vector_near_duplicate_catches_reordered_wording(user):
    await store.save_memory(memory(user, "Prefers quiet cafes for deep work", kind="preference"))
    await store.save_memory(memory(user, "For deep work prefers quiet cafes", kind="preference"))
    assert len(await active_contents(user)) == 1


async def test_different_facts_are_not_merged(user):
    await store.save_memory(memory(user, "Lives in Pune"))
    await store.save_memory(memory(user, "Lives in Kolkata"))
    await store.save_memory(memory(user, "Does not like coffee"))
    await store.save_memory(memory(user, "Does like coffee"))
    assert len(await active_contents(user)) == 4


async def test_same_wording_different_kind_or_date_is_not_merged(user):
    march = datetime(2031, 3, 10, 10, tzinfo=timezone.utc)
    april = datetime(2031, 4, 12, 10, tzinfo=timezone.utc)
    await store.save_memory(memory(user, "Dentist appointment at 10am", kind="event", event_at=march))
    await store.save_memory(memory(user, "Dentist appointment at 10am.", kind="event", event_at=april))
    await store.save_memory(memory(user, "Dentist appointment at 10am..", kind="fact"))
    assert len(await active_contents(user)) == 3


# ---------- embedding drift ----------

async def test_save_records_which_model_made_the_vector(user):
    saved = await store.save_memory(memory(user, "Likes long walks by the river", kind="preference"))
    async with session_scope() as session:
        row = await session.get(Memory, saved.id)
        assert row.embedding_model == settings.embedding_model_id and row.embedded_at is not None


async def test_new_model_hides_old_vectors_until_reembed(user, monkeypatch):
    saved = await store.save_memory(memory(user, "Prefers quiet cafes for deep focused work", kind="preference"))
    query = plan(semantic_query="quiet cafes for focused work")
    assert [m.id for m in await retrieval.hybrid_search(user.id, query)] == [saved.id]

    monkeypatch.setattr(settings, "embedding_model_id", "some.new-model:1")
    assert await retrieval.hybrid_search(user.id, query) == []  # old-model vectors are never compared
    health = await maintenance.memory_health(user.id)
    assert health["needs_embedding"] == 1 and not health["healthy"]

    # (the maintenance module and retrieval both call the patched `embed`)
    monkeypatch.setattr(maintenance, "embed", fake_embed)
    assert await maintenance.reembed_stale(user.id) == {"embedded": 1, "failed": 0}
    assert (await maintenance.memory_health(user.id))["healthy"]
    assert [m.id for m in await retrieval.hybrid_search(user.id, query)] == [saved.id]


async def test_embedding_outage_still_saves_and_reembed_repairs_it(user, monkeypatch):
    async def broken(text):
        raise RuntimeError("bedrock down")

    monkeypatch.setattr(store, "embed", broken)
    saved = await store.save_memory(memory(user, "Prefers window seats on trains", kind="preference"))
    async with session_scope() as session:
        wants, no_vector, model = (
            await session.execute(
                select(Memory.wants_embedding, Memory.embedding.is_(None), Memory.embedding_model).where(
                    Memory.id == saved.id
                )
            )
        ).one()
        assert wants and no_vector and model is None

    monkeypatch.setattr(maintenance, "embed", fake_embed)
    assert (await maintenance.reembed_stale(user.id))["embedded"] == 1
    assert (await maintenance.memory_health(user.id))["healthy"]


async def test_reembed_counts_failures_and_does_not_loop(user, monkeypatch):
    async def broken(text):
        raise RuntimeError("bedrock down")

    monkeypatch.setattr(store, "embed", broken)
    await store.save_memory(memory(user, "Prefers window seats on trains", kind="preference"))
    monkeypatch.setattr(maintenance, "embed", broken)
    assert await maintenance.reembed_stale(user.id) == {"embedded": 0, "failed": 1}


async def test_database_refuses_a_vector_with_no_model_name(user):
    saved = await store.save_memory(memory(user, "Likes tea", kind="preference"))
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            await session.execute(text("UPDATE memories SET embedding_model = NULL WHERE id = :i"), {"i": saved.id})


async def test_dimension_check(monkeypatch, migrated_database):
    from app.db import engine

    await engine.dispose()  # this test has its own event loop
    try:
        await assert_embedding_dimension()  # matches the schema
        monkeypatch.setattr(settings, "embedding_dim", 512)
        with pytest.raises(RuntimeError, match="EMBEDDING_DIM"):
            await assert_embedding_dimension()
    finally:
        await engine.dispose()


# ---------- re-extraction from the archive ----------

async def test_reextract_replays_archive_and_dry_run_saves_nothing(user, tmp_path, monkeypatch):
    archive = LocalArchive(str(tmp_path))
    key = f"users/{user.id}/2031/03/01/conv/turn1.json"
    await archive.put_json(key, {"at": "2031-03-01T09:00:00+00:00", "user": "My sister is Riya", "assistant": "Nice"})
    monkeypatch.setattr(maintenance, "get_archive", lambda: archive)

    seen_at = []

    async def fake_extract(user_, said, reply, known, turn_key, at=None):
        seen_at.append(at)
        return [MemoryCreate(user_id=user_.id, content="Riya is Subhadip's sister", source_key=turn_key)]

    monkeypatch.setattr(maintenance, "extract_memories", fake_extract)

    dry = await maintenance.reextract_archive(user, dry_run=True)
    assert dry["proposed"] == 1 and dry["saved"] == 0 and await active_contents(user) == []

    real = await maintenance.reextract_archive(user)
    again = await maintenance.reextract_archive(user)  # idempotent
    assert real["saved"] == 1 and again["failed"] == 0
    assert await active_contents(user) == ["Riya is Subhadip's sister"]
    assert seen_at[0].year == 2031  # replayed with the turn's own time

    assert (await maintenance.reextract_archive(user, since=datetime(2032, 1, 1).date()))["turns"] == 0


async def test_archive_lists_keys_in_date_order(tmp_path):
    archive = LocalArchive(str(tmp_path))
    for day in ("02", "01"):
        await archive.put_json(f"users/u/2031/03/{day}/c/x.json", {})
    assert await archive.list_keys("users/u/") == ["users/u/2031/03/01/c/x.json", "users/u/2031/03/02/c/x.json"]
    assert await archive.list_keys("users/none/") == []


# ---------- ranking ----------

def test_weights_change_the_winner():
    keyword, semantic = ["a", "b"], ["b", "a"]
    assert retrieval.reciprocal_rank_fusion([keyword, semantic], [2.0, 1.0])[0] == "a"
    assert retrieval.reciprocal_rank_fusion([keyword, semantic], [1.0, 2.0])[0] == "b"


def test_importance_boost_is_a_small_multiplier():
    assert retrieval.importance_boost(3, 0.05) == 1.0
    assert retrieval.importance_boost(5, 0.05) == pytest.approx(1.1)
    assert retrieval.importance_boost(1, 0.05) == pytest.approx(0.9)


async def test_importance_breaks_a_tie(user):
    low = await store.save_memory(memory(user, "Riya likes tea", importance=1))
    high = await store.save_memory(memory(user, "Riya likes jazz", importance=5))
    found = await retrieval.hybrid_search(user.id, plan(keywords=["Riya"]))
    assert found[0].id == high.id and {m.id for m in found} == {low.id, high.id}


def test_rerank_order_ignores_bad_numbers_and_loses_nothing():
    items = ["a", "b", "c"]
    assert apply_order(items, [3, 3, 9, 0, 1]) == ["c", "a", "b"]
    assert apply_order(items, []) == items


async def test_reranker_failure_keeps_original_order(monkeypatch):
    from app.memory import rerank

    def broken(fast=False, **kwargs):
        raise RuntimeError("no model")

    monkeypatch.setattr(rerank, "chat_model", broken)

    class M:
        def __init__(self, content):
            self.content = content

    items = [M("a"), M("b")]
    assert await rerank.rerank("q", items) == items


def test_eval_scoring():
    case = EvalCase(query="q", expect=["Acme"])
    assert score_case(case, ["x", "Works at acme"]) == 0.5
    assert score_case(case, ["x"]) == 0.0
