"""Telemetry: traces and spans, the model/tool callback, retrieval reports, the /obs API and its safety rules.
Needs the Postgres from docker-compose (skipped when it is not running)."""

import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from sqlalchemy import delete, select, text

from app.config import settings
from app.db import engine, session_scope
from app.main import app
from app.memory import retrieval, store
from app.memory.schemas import EntityMention, MemoryCreate, RetrievalPlan
from app.models import ObsSpan, ObsTrace
from app.obs import logs, queries
from app.obs import trace as obs
from app.obs.callbacks import ObsCallbackHandler
from app.obs.pricing import cost_usd
from tests.fakes import ScriptedChatModel

MARK = "pytest-obs-"


@pytest.fixture
async def clean(migrated_database):
    """Remove everything these tests write, whatever happens."""
    yield
    async with session_scope() as session:
        await session.execute(delete(ObsTrace).where(ObsTrace.user_text.like(MARK + "%")))
        await session.execute(delete(ObsSpan).where(text("attrs->>'pytest' = '1'")))
    await engine.dispose()


@pytest.fixture
async def client(clean):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


def marker() -> str:
    return MARK + uuid.uuid4().hex[:10]


async def trace_row(trace_id):
    async with session_scope() as session:
        return await session.get(ObsTrace, trace_id)


async def span_rows(trace_id):
    async with session_scope() as session:
        return (await session.scalars(select(ObsSpan).where(ObsSpan.trace_id == trace_id).order_by(ObsSpan.started_at))).all()


# ---------------------------------------------------------------- pricing + clipping
def test_cost_estimates():
    assert cost_usd("us.anthropic.claude-sonnet-4-5-20250929-v1:0", 1_000_000, 1_000_000) == pytest.approx(18.0)
    assert cost_usd("us.anthropic.claude-haiku-4-5-20251001-v1:0", 1_000_000, 0) == pytest.approx(1.0)
    assert cost_usd("amazon.titan-embed-text-v2:0", 1_000_000, 0) == pytest.approx(0.02)
    assert cost_usd("some-unknown-model", 5000, 5000) == 0
    assert cost_usd(None, 1, 1) == 0


def test_content_is_dropped_when_storage_is_off(monkeypatch):
    assert obs.clip("secret words") == "secret words"
    assert obs.clip("x" * 3000).endswith("[+1000 chars]")
    monkeypatch.setattr(settings, "telemetry_store_content", False)
    assert "secret" not in obs.clip("secret words")
    assert obs.clip(None) is None


# ---------------------------------------------------------------- traces and spans
async def test_trace_with_spans_is_written_in_one_go(clean):
    trace = obs.start("chat", "web", marker())
    with obs.span("tool", "add_task", args="{}") as s:
        s.attrs["pytest"] = "1"
    with obs.span("llm", "supervisor") as s:
        s.model, s.input_tokens, s.output_tokens = "us.anthropic.claude-sonnet-4-5-x", 1000, 100
    trace.reply_text, trace.recalled = "ok", 3
    await obs.finish(trace)

    row = await trace_row(trace.id)
    assert (row.kind, row.source, row.status, row.llm_calls, row.tool_calls, row.recalled) == ("chat", "web", "ok", 1, 1, 3)
    assert row.input_tokens == 1000 and row.output_tokens == 100 and row.cost_usd > 0
    assert [s.kind for s in await span_rows(trace.id)] == ["tool", "llm"]
    assert trace.id not in obs.ACTIVE and obs.current() is None


async def test_a_failing_block_marks_its_span_and_still_raises(clean):
    trace = obs.start("chat", "api", marker())
    with pytest.raises(ValueError):
        with obs.span("tool", "boom"):
            raise ValueError("nope")
    trace.fail("turn failed")
    await obs.finish(trace)
    (span,) = await span_rows(trace.id)
    assert span.status == "error" and "nope" in span.error and span.attrs["error_type"] == "ValueError"
    assert (await trace_row(trace.id)).status == "error"


async def test_telemetry_can_be_switched_off(clean, monkeypatch):
    monkeypatch.setattr(settings, "telemetry_enabled", False)
    assert obs.start("chat", "api", marker()) is None
    await obs.finish(None)  # harmless


async def test_a_database_failure_never_reaches_the_caller(clean, monkeypatch):
    async def broken(trace):
        raise RuntimeError("db down")

    monkeypatch.setattr(obs, "_write", broken)
    trace = obs.start("chat", "api", marker())
    await obs.finish(trace)  # must not raise


# ---------------------------------------------------------------- LangChain callback
async def test_model_calls_are_recorded_with_tokens(clean):
    handler = ObsCallbackHandler()
    usage = {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}
    model = ScriptedChatModel(script=[AIMessage(content="hello back", usage_metadata=usage)], callbacks=[handler], tags=["purpose:recall_plan"])
    trace = obs.start("chat", "api", marker())
    await model.ainvoke("hello")
    await obs.finish(trace)
    (span,) = await span_rows(trace.id)
    assert span.kind == "llm" and span.name == "recall_plan"
    assert (span.input_tokens, span.output_tokens) == (120, 30)
    assert span.attrs["output"] == "hello back" and span.attrs["last_input"] == "hello"


async def test_tool_calls_are_recorded_and_failures_flagged(clean):
    handler = ObsCallbackHandler()

    @tool
    async def good(x: int) -> str:
        """adds one"""
        return str(x + 1)

    @tool
    async def bad(x: int) -> str:
        """always fails"""
        raise RuntimeError("tool exploded")

    trace = obs.start("chat", "api", marker())
    assert await good.ainvoke({"x": 1}, config={"callbacks": [handler]}) == "2"
    with pytest.raises(RuntimeError):
        await bad.ainvoke({"x": 1}, config={"callbacks": [handler]})
    await obs.finish(trace)
    ok, failed = await span_rows(trace.id)
    assert (ok.name, ok.status, ok.attrs["result"]) == ("good", "ok", "2")
    assert (failed.name, failed.status) == ("bad", "error") and "tool exploded" in failed.error


async def test_the_same_handler_reached_twice_counts_once(clean):
    handler = ObsCallbackHandler()
    model = ScriptedChatModel(script=[AIMessage(content="x")], callbacks=[handler])
    trace = obs.start("chat", "api", marker())
    await model.ainvoke("hi", config={"callbacks": [handler]})  # model level and run config
    await obs.finish(trace)
    assert len(await span_rows(trace.id)) == 1


# ---------------------------------------------------------------- retrieval report
async def test_hybrid_search_reports_channels_and_ranking(user, clean):
    await store.save_memory(MemoryCreate(user_id=user.id, content="Riya is Subhadip's sister", entities=[EntityMention(name="Riya", type="person")]))
    trace = obs.start("chat", "api", marker())
    found = await retrieval.hybrid_search(user.id, RetrievalPlan(needs_memory=True, keywords=["Riya"], entities=["Riya"]))
    await obs.finish(trace)
    assert len(found) == 1
    (span,) = await span_rows(trace.id)
    a = span.attrs
    assert span.kind == "retrieval" and a["returned"] == 1 and a["candidates"] == 1
    assert set(a["channels"]) == {"keyword", "graph"} and a["channels"]["keyword"]["hits"] == 1
    (result,) = a["results"]
    assert result["rank"] == 1 and set(result["found_by"]) == {"keyword", "graph"} and result["rrf"] > 0
    assert "Riya" in result["text"]


async def test_a_failing_channel_is_reported_not_raised(user, clean, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("bedrock down")

    monkeypatch.setattr(retrieval, "semantic_search", broken)
    trace = obs.start("chat", "api", marker())
    found = await retrieval.hybrid_search(user.id, RetrievalPlan(needs_memory=True, semantic_query="anything"))
    await obs.finish(trace)
    assert found == []
    (span,) = await span_rows(trace.id)
    assert span.attrs["channels"]["semantic"]["error"] == "RuntimeError" and span.attrs["returned"] == 0


# ---------------------------------------------------------------- the API
async def seed_trace(status="ok", ms=1234, kind="chat", started=None, text_=None) -> uuid.UUID:
    text_ = text_ or marker()
    trace_id = uuid.uuid4()
    started = started or datetime.now(timezone.utc)
    async with session_scope() as session:
        session.add(ObsTrace(id=trace_id, kind=kind, source="web", started_at=started, duration_ms=ms, status=status,
                             error="boom" if status == "error" else None, user_text=text_, reply_text="reply",
                             input_tokens=10, output_tokens=5, llm_calls=1, cost_usd=0.001, meta={}))
        await session.flush()
        session.add(ObsSpan(trace_id=trace_id, kind="llm", name="supervisor", started_at=started, duration_ms=ms, model="m",
                            input_tokens=10, output_tokens=5, attrs={"pytest": "1"}))
    return trace_id


async def test_every_endpoint_answers(client):
    for path in ["/obs/overview?hours=1", "/obs/overview?hours=168", "/obs/alerts", "/obs/traces", "/obs/llm", "/obs/tools",
                 "/obs/retrieval", "/obs/memory", "/obs/operator", "/obs/db", "/obs/logs", "/obs/system", "/dashboard/"]:
        response = await client.get(path)
        assert response.status_code == 200, (path, response.text[:200])


async def test_traces_are_listed_filtered_and_opened(client):
    text_ = marker()
    ok = await seed_trace(text_=text_ + " fine")
    bad = await seed_trace(status="error", text_=text_ + " broken")
    listing = (await client.get("/obs/traces", params={"q": text_})).json()
    assert listing["total"] == 2
    errors = (await client.get("/obs/traces", params={"q": text_, "status": "error"})).json()
    assert [r["id"] for r in errors["rows"]] == [str(bad)]
    detail = (await client.get(f"/obs/traces/{ok}")).json()
    assert detail["trace"]["user_text"].endswith("fine") and detail["spans"][0]["offset_ms"] == 0
    assert (await client.get(f"/obs/traces/{uuid.uuid4()}")).status_code == 404


async def test_overview_counts_new_traffic(client):
    before = (await client.get("/obs/overview", params={"hours": 1})).json()["current"]["requests"]
    await seed_trace(ms=500)
    await seed_trace(ms=900, status="error")
    after = (await client.get("/obs/overview", params={"hours": 1})).json()
    assert after["current"]["requests"] == before + 2 and after["current"]["errors"] >= 1
    assert after["series"] and all("t" in point for point in after["series"])


async def test_a_run_of_failures_raises_an_alert(client):
    for _ in range(6):
        await seed_trace(status="error")
    alerts = (await client.get("/obs/alerts")).json()
    chat = [a for a in alerts if a["id"] == "chat-errors"]
    assert chat and chat[0]["severity"] in ("warn", "critical")


async def test_database_browser_is_read_only_and_whitelisted(client, user):
    await store.save_memory(MemoryCreate(user_id=user.id, content="Prefers quiet cafes for deep work", kind="preference"))
    tables = {t["name"] for t in (await client.get("/obs/db")).json()}
    assert {"memories", "messages", "obs_traces"} <= tables
    page = (await client.get("/obs/db/memories", params={"q": "quiet cafes"})).json()
    assert page["total"] >= 1 and "embedding" not in page["columns"] and "has_embedding" in page["columns"]
    assert (await client.get("/obs/db/pg_user")).status_code == 404  # not a browsable table
    assert (await client.get("/obs/db/alembic_version")).status_code == 404
    hostile = await client.get("/obs/db/memories", params={"sort": "id; DROP TABLE users", "q": "' OR 1=1 --"})
    assert hostile.status_code == 200 and hostile.json()["sort"] == "created_at"  # falls back to a real column
    async with session_scope() as session:
        assert await session.scalar(text("SELECT count(*) FROM users")) is not None  # still there


async def test_endpoints_need_the_key_when_one_is_set(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "s3cret")
    assert (await client.get("/obs/overview")).status_code == 401
    assert (await client.get("/obs/overview", headers={"X-API-Key": "wrong"})).status_code == 401
    assert (await client.get("/obs/overview", headers={"X-API-Key": "s3cret"})).status_code == 200
    assert (await client.get("/obs/db/memories")).status_code == 401


async def test_old_traces_are_purged(clean, monkeypatch):
    from app.obs import health

    monkeypatch.setattr(settings, "telemetry_retention_days", 30)
    old = await seed_trace(started=datetime.now(timezone.utc) - timedelta(days=45))
    fresh = await seed_trace()
    await health.purge_old()
    assert await trace_row(old) is None and await trace_row(fresh) is not None


def test_zero_fill_keeps_gaps_for_percentiles():
    since = datetime(2030, 1, 1, tzinfo=timezone.utc)
    until = since + timedelta(minutes=5)
    filled = queries.zero_fill([{"t": since, "n": 4, "p95": 120.0}], since, until, 60, ["n", "p95"])
    assert [p["n"] for p in filled] == [4, 0, 0, 0, 0, 0] and filled[0]["p95"] == 120 and filled[1]["p95"] is None


# ---------------------------------------------------------------- logs and secrets
def test_log_buffer_filters_and_hides_secrets():
    ring = logs.RingLogHandler(capacity=10)
    log = logging.getLogger("pytest.ring")
    log.addHandler(ring)
    log.setLevel(logging.DEBUG)
    try:
        log.info("POST https://api.telegram.org/bot1234567890:AAFakeTokenForTestingPurposesOnly1234/getUpdates 200")
        log.warning("slow thing happened")
        log.error("headers x-api-key: sk_live_abcdef123456 rejected")
    finally:
        log.removeHandler(ring)
    everything = ring.query("INFO")
    assert len(everything) == 3
    joined = " ".join(r["message"] for r in everything)
    assert "AAFake" not in joined and "sk_live_abcdef" not in joined and "bot***" in joined
    assert [r["level"] for r in ring.query("WARNING")] == ["WARNING", "ERROR"]
    assert len(ring.query("INFO", q="slow")) == 1
    assert ring.counts() == {"WARNING": 1, "ERROR": 1, "CRITICAL": 0}
