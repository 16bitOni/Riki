"""A full chat round trip through the real API and the real database; only the LLM is scripted."""

import json
from uuid import uuid4

import httpx
from langchain_core.messages import AIMessage
from sqlalchemy import delete

from app import chat_service
from app.agents import graph as graph_module
from app.api import routes
from app.config import settings
from app.db import engine, session_scope
from app.main import app
from app.models import User
from app.storage import get_archive
from tests.fakes import ScriptedChatModel


async def test_chat_round_trip(migrated_database, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "user_name", "pytest-chat-user")
    monkeypatch.setattr(settings, "local_archive_dir", str(tmp_path))
    get_archive.cache_clear()

    async def no_memories(user, message, recent_chat):
        return "## About the user\n(nothing yet)", []

    model = ScriptedChatModel(script=[AIMessage(content="Hello there!"), AIMessage(content="Still here.")])
    monkeypatch.setattr(graph_module, "build_memory_context", no_memories)
    monkeypatch.setattr(chat_service, "get_graph", lambda: graph_module.build_graph(model))

    extraction_calls = []

    async def record_extraction(*args):
        extraction_calls.append(args)

    monkeypatch.setattr(routes, "extract_and_save", record_extraction)

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post("/chat", json={"message": "  Hi  "})
            assert first.status_code == 200
            conversation_id = first.json()["conversation_id"]
            assert first.json()["reply"] == "Hello there!"

            second = await client.post(
                "/chat", json={"message": "Anyone there?", "conversation_id": conversation_id}
            )
            assert second.json()["reply"] == "Still here."

            messages = (await client.get(f"/conversations/{conversation_id}/messages")).json()
            assert [(m["role"], m["content"]) for m in messages] == [
                ("user", "Hi"),  # whitespace was trimmed by validation
                ("assistant", "Hello there!"),
                ("user", "Anyone there?"),
                ("assistant", "Still here."),
            ]

            assert (await client.post("/chat", json={"message": "x", "conversation_id": str(uuid4())})).status_code == 404

        # tier 1: each turn was archived, and extraction was handed the archive key
        archived = [json.loads(p.read_text()) for p in tmp_path.rglob("*.json")]
        assert sorted(a["user"] for a in archived) == ["Anyone there?", "Hi"]
        assert len(extraction_calls) == 2
    finally:
        async with session_scope() as session:
            await session.execute(delete(User).where(User.name == "pytest-chat-user"))
        await engine.dispose()
        get_archive.cache_clear()


async def test_chat_turns_are_traced(migrated_database, monkeypatch, tmp_path):
    """Both the plain and the streaming endpoint leave a trace with the model call and the right source."""
    from uuid import UUID

    from sqlalchemy import select

    from app.models import ObsTrace, ObsSpan

    monkeypatch.setattr(settings, "user_name", "pytest-trace-user")
    monkeypatch.setattr(settings, "local_archive_dir", str(tmp_path))
    get_archive.cache_clear()

    async def no_memories(user, message, recent_chat):
        return "## About the user\n(nothing yet)", []

    usage = {"input_tokens": 50, "output_tokens": 7, "total_tokens": 57}
    model = ScriptedChatModel(script=[AIMessage(content="Plain reply", usage_metadata=usage), AIMessage(content="Streamed reply", usage_metadata=usage)])
    monkeypatch.setattr(graph_module, "build_memory_context", no_memories)
    monkeypatch.setattr(chat_service, "get_graph", lambda: graph_module.build_graph(model))

    async def skip_extraction(*args):
        return None

    monkeypatch.setattr(routes, "extract_and_save", skip_extraction)

    conversation_ids = []
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            plain = (await client.post("/chat", json={"message": "pytest-obs-plain"})).json()
            conversation_ids.append(UUID(plain["conversation_id"]))
            streamed = await client.post("/chat/stream", json={"message": "pytest-obs-stream"})
            assert '"done": true' in streamed.text

        async with session_scope() as session:
            traces = (await session.scalars(select(ObsTrace).where(ObsTrace.user_text.like("pytest-obs-%")).order_by(ObsTrace.started_at))).all()
            assert [(t.kind, t.source, t.status) for t in traces] == [("chat", "api", "ok"), ("chat", "web", "ok")]
            assert [t.reply_text for t in traces] == ["Plain reply", "Streamed reply"]
            for t in traces:
                assert t.llm_calls >= 1 and t.input_tokens == 50 and t.output_tokens == 7 and t.duration_ms > 0
                spans = (await session.scalars(select(ObsSpan).where(ObsSpan.trace_id == t.id))).all()
                assert any(s.kind == "llm" for s in spans)
    finally:
        async with session_scope() as session:
            await session.execute(delete(ObsTrace).where(ObsTrace.user_text.like("pytest-obs-%")))
            await session.execute(delete(User).where(User.name == "pytest-trace-user"))
        await engine.dispose()
        get_archive.cache_clear()
