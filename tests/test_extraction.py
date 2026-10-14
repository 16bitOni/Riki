"""The background extractor: LLM proposals are validated, filtered and saved."""

from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import select

from app.db import session_scope
from app.memory import extraction, store
from app.memory.schemas import Extraction, MemoryCreate, NewMemory
from app.models import Memory


class FakeLLM:
    """Stands in for chat_model(): returns a fixed Extraction from .with_structured_output(...).ainvoke(...)."""

    def __init__(self, result):
        self.result = result

    def with_structured_output(self, schema):
        return self

    async def ainvoke(self, messages):
        return self.result


async def test_extractor_saves_valid_memories_and_skips_invalid_ones(user, monkeypatch):
    old = await store.save_memory(MemoryCreate(user_id=user.id, content="Lives in Pune"))
    stranger_id = "11111111-1111-1111-1111-111111111111"  # never shown to the model

    proposed = Extraction(
        memories=[
            NewMemory(  # valid, and retires a memory the model was shown
                content="Lives in Kolkata", kind="fact", importance=4, semantic=False,
                replaces=[str(old.id), stranger_id],
            ),
            NewMemory(  # importance out of range: must be skipped, not saved
                content="Something odd", kind="fact", importance=9, semantic=False
            ),
            NewMemory(  # no timezone on the date: treated as the user's local time
                content="Flight to Delhi", kind="event", importance=3, semantic=False,
                event_at=datetime(2031, 5, 1, 9, 0),
            ),
        ]
    )
    monkeypatch.setattr(extraction, "chat_model", lambda fast=False, **kwargs: FakeLLM(proposed))

    async def no_profile_refresh(_user):
        return None

    monkeypatch.setattr(extraction, "refresh_profile_if_stale", no_profile_refresh)

    me = SimpleNamespace(id=user.id, name="Test User", timezone="Asia/Kolkata", profile="", profile_updated_at=None)
    await extraction.extract_and_save(me, "I moved", "Noted", known=[old], turn_key="turn-1")

    async with session_scope() as session:
        rows = (await session.scalars(select(Memory).where(Memory.user_id == user.id))).all()
    by_content = {m.content: m for m in rows}

    assert "Something odd" not in by_content
    assert by_content["Lives in Kolkata"].source_key == "turn-1"
    assert by_content["Lives in Pune"].is_active is False  # retired
    assert by_content["Flight to Delhi"].event_at.utcoffset().total_seconds() == 0  # stored in UTC
    assert by_content["Flight to Delhi"].event_at.hour == 3  # 09:00 IST == 03:30 UTC
